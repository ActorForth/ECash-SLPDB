"""
slp pre-flight burn check for all transactions to provide a secondary check
for slp validity and burn prevention.
"""

import sys, json
import threading
import requests
import codecs
import base64
import random

from electroncash import networks
from .slp import SlpMessage

BCHD_RESP_NON_SLP_WITH_BURNS = 'non-slp transaction, includes valid slp inputs'
BCHD_RESP_NON_SLP_OK = 'non-slp transaction'

class SlpPreflightCheck:
    @staticmethod
    def query(tx, *, selected_slp_coins=[], amt_to_burn=0, cb=None):
        """
        Uses bchd full nodes to perform a final check for both non-slp and slp transactions
        before broadcasting to the network.
        
        This method is blocking, so it should be called from a broadcast_thread.

        If token burns are desired they must be explicitly specified.  Depending on
        the format for the burn request will vary as follows:

            * Burning tokens of the same token id in a valid slp transaction requires:
                { "token_id": "base64_string",
                    "token_type": "VERSION_NOT_SET",
                    "amount": number
                }

            * Burning tokens as part of a non-slp transaction requires:
                { "outpoint": {
                        "hash": "base64_string_of_reversed_txid",
                        "index": number
                    },
                    "token_id": "base64_string",
                    "token_type": number,
                    "amount": number,
                }

            * Burning a mint baton requires:
                { "outpoint": {
                        "hash": "base64_string_of_reversed_txid",
                        "index": number
                    },
                    "token_id": "base64_string",
                    "token_type": number,
                    "mint_baton_vout": number
                }

        """

        # put in a dummy scriptSig before serializing to avoid
        # weird EC specific serialization when transaction is 
        # unsigned.
        sigs = []
        for inp in tx.inputs():
            sigs.append(inp['signatures'])
            inp['signatures'] = ['00']

        # serialize the transaction
        b64_transaction = base64.standard_b64encode(
                            codecs.decode(tx.serialize(), 'hex')
                            ).decode("ascii")

        # restore transaction's original scriptSigs.  Normally 
        # unsigned transactions will be sent to this method, so
        # these values should be [None], but in case for some reason
        # the transaction is already signed, then it will restore an
        # actual signature value.
        for i, inp in enumerate(tx.inputs()):
            inp['signatures'] = sigs[i]


        # reformat selected_slp_coins into the required json format for bchd-proxy
        required_burns = []
        if selected_slp_coins:
            token_id_hex = selected_slp_coins[0]['token_id_hex']

            # check all of the proposed token burns have the same token id
            for coin in selected_slp_coins:
                assert token_id_hex == coin['token_id_hex']

            # check to see if this transaction is slp or not
            try:
                slp_msg = SlpMessage.parseSlpOutputScript(tx.outputs()[0][1])
                is_slp = True
            except:
                is_slp = False

            required_burns = SlpPreflightCheck._marshal_burn_requests(selected_slp_coins, amt_to_burn, is_slp)            

        query_json = {
            'transaction': b64_transaction,          # the serialized transaction to check
            'required_slp_burns': required_burns,    # see required format above
            'use_spec_validity_judgement': False     # using safe judgement to avoid any required_burns
        }

        BCHD_NODES = [ host for host in networks.net.SLP_GS_SERVERS if networks.net.SLP_GS_SERVERS[host]['kind'] == 'bchd']
        random.shuffle(BCHD_NODES)

        resp = { 'ok': False, 'invalid_reason': 'no bchd nodes available' }

        # try to get a response from one of the bchd nodes
        for node in BCHD_NODES:
            try:
                reqresult = requests.post(node + '/v1/CheckSlpTransaction', json=query_json, timeout=1)
                resp = json.loads(reqresult.content.decode('utf-8'))
            except Exception as e: # this happens when bchd gateway proxy is down
                print('slp pre-flight check failed for %s:\n%s'%(node, e), file=sys.stderr)
                continue
            else:
                if resp.get('is_valid'):             # this happens when bchd says all is good.
                    resp['ok'] = True
                    break
                elif resp.get('invalid_reason') == \
                        BCHD_RESP_NON_SLP_WITH_BURNS: # this happens which non-slp transaction has slp inputs
                    resp['ok'] = False
                    break
                elif resp.get('invalid_reason') == \
                        BCHD_RESP_NON_SLP_OK:  # this happens when transaction is non-slp, with no burn problems
                    resp['ok'] = True
                    break
                elif resp.get('is_valid') == False:  # this happens when bchd says something isn't right.
                    resp['ok'] = False
                    break
                elif resp.get('code'):               # this happens when bchd proxy returns an error 
                    resp['ok'] = False               # (e.g, bchd grpc is down, or can't deserialize txn)
                    resp['invalid_reason'] = resp.get('message')
                    break
                continue                             # this happens when the server returns an unexpected kind of response

        if cb: cb(resp)
        return resp

    @staticmethod
    def _marshal_burn_requests(selected_slp_coins, desired_burn_amt, is_slp_txn):
        token_id = base64.standard_b64encode(codecs.decode(selected_slp_coins[0]['token_id_hex'], 'hex')).decode("ascii")
        token_type_int = int(selected_slp_coins[0]['token_type'].split("SLP")[1])
        burns = []
        non_slp_burn = not is_slp_txn

        # if the burn amount is being burned within a
        # valid slp transaction then we don't need to specify
        # a specific outpoint, only the amount being burned
        # i.e., inputs-outputs
        if not non_slp_burn and desired_burn_amt>0:
            burns.append({
                'token_id': token_id,
                'token_type': token_type_int,
                'amount': desired_burn_amt
            })

        # loop through the coins to look for mint baton and
        # token burns to happen in a non-slp transaction
        for coin in selected_slp_coins:
            prevhash = base64.standard_b64encode(codecs.decode(coin['prevout_hash'], 'hex')[::-1]).decode("ascii")
            if coin['token_value'] == 'MINT_BATON':
                burns.append({
                    'outpoint': {
                        'hash': prevhash,  
                        'index': coin['prevout_n']
                    },
                    'token_id': token_id,
                    'token_type': token_type_int,
                    'mint_baton_vout': coin['prevout_n']
                })
                continue
            elif non_slp_burn:
                burns.append({
                    'outpoint': {
                        'hash': prevhash,
                        'index': coin['prevout_n']
                    },
                    'token_id': token_id,
                    'token_type': token_type_int,
                    'amount': coin['token_value'],
                })
                continue
            elif desired_burn_amt == 0:
                continue
            elif not non_slp_burn:
                continue
            raise Exception("Unhandled coin to burn")
        
        return burns