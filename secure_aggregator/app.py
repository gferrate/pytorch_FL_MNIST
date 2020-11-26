import sys; sys.path.insert(0, '.')
from flask import Flask, request, jsonify, abort

import requests, json, os
import argparse
import ntpath

from shared import utils
from sec_agg import SecAgg
from shared.state import (
    SEC_AGG_SEND_TO_MAIN_SERVER,
    SEC_AGG_AGGREGATE_MODELS,
    SEC_AGG_GET_CLIENT_MODEL,
    State
)
from shared import rsa_utils


parser = argparse.ArgumentParser(description='PyTorch FL MNIST Example')
parser.add_argument('-p', '--port', type=str, required=True,
                    help='Client port. Example: 8001')

rsa = rsa_utils.RSAUtils()
args = parser.parse_args()
hosts = utils.read_hosts()

use_cuda = True
sec_agg = SecAgg(args.port, use_cuda)
state = State('secure_aggregator', sec_agg.client_id, args.port)


app = Flask(__name__)


def assert_idle_state(func):
    def wrapper():
        if not state.is_idle():
            msg = (
                'Application not in IDLE state. '
                'Current state: {}'.format(state.current_state)
            )
            abort(404, description=msg)
        return func()
    return wrapper


@app.route('/')
def index():
    return jsonify({'running': 1})


@app.route('/pub_key')
def get_pub_key():
    return jsonify({'pub_key': rsa.export_public_key()})


@assert_idle_state
@app.route('/client_model', methods=['POST'])
def get_client_model():
    state.current_state = SEC_AGG_GET_CLIENT_MODEL
    # file = request.files['model'].read()
    enc_data = rsa.get_crypt_files_from_req(request)
    file = rsa.decrypt_bytes(enc_data)
    data = request.files['json'].read()
    data = json.loads(data.decode('utf-8'))
    client_id = data['client_id']
    fname = '{}_{}'.format(client_id, 'model.tar')
    fname = 'secure_aggregator/client_models/{}'.format(fname)
    if not os.path.exists(os.path.dirname(fname)):
        os.makedirs(os.path.dirname(fname))
    with open(fname, 'wb') as f:
        f.write(file)
    state.idle()
    return jsonify({'msg': 'Model received', 'location': fname})


@assert_idle_state
@app.route('/aggregate_models')
def perform_model_aggregation():
    state.current_state = SEC_AGG_AGGREGATE_MODELS
    # Test: Init model in each model aggregation to restart the epoch numbers
    sec_agg.init_model()
    sec_agg.aggregate_models()
    # TODO: Maybe we could save the model and continue the process before
    # doing the test so the clients can do more work in less time
    test_result = sec_agg.test()
    sec_agg.save_model()
    # This is only to make sure that no aggregation is repeated
    sec_agg.delete_client_models()
    state.idle()
    return jsonify({
        'msg': ('Model aggregation done!\n'
                'Global model written to persistent storage.'),
        'test_result': test_result
    })


@assert_idle_state
@app.route('/send_model_to_main_server')
def send_agg_to_mainserver():
    state.current_state = SEC_AGG_SEND_TO_MAIN_SERVER
    path = sec_agg.get_model_filename()
    model_byte_array = open(path, "rb").read()
    host = hosts['main_server']['host']
    port = hosts['main_server']['port']
    enc_session_key, nonce, tag, ciphertext = \
        rsa.encrypt_bytes(model_byte_array, host=host, port=port)
    data = {'fname': path, 'id': 'sec_agg'}
    files = {
        'json': ('json_data', json.dumps(data), 'application/json'),
        'enc_session_key': ('sk', enc_session_key, 'application/octet-stream'),
        'nonce': ('nonce', nonce, 'application/octet-stream'),
        'tag': ('tag', tag, 'application/octet-stream'),
        'ciphertext': ('ciphertext', ciphertext, 'application/octet-stream'),
    }
    url = 'http://{}:{}/secagg_model'.format(host, port)
    req = requests.post(url=url, files=files)
    state.idle()
    if req.status_code == 200:
        return jsonify({'msg': 'Aggregated model sent to main server'})
    return abort(404, description='Something went wrong')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=sec_agg.port, debug=False, use_reloader=False)
