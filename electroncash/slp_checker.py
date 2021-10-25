import json
from .util import NotEnoughFundsSlp, NotEnoughUnfrozenFundsSlp, print_error
from . import slp, networks
from .slp import SlpParsingError, SlpInvalidOutputMessage, SlpUnsupportedSlpTokenType
from .slp_preflight_check import SlpPreflightCheck
from .transaction import Transaction
from .address import Address

class SlpTransactionChecker:
    @staticmethod
    def check_tx_slp(wallet, tx, *, coins_to_burn=None, amt_to_burn=None, require_tx_in_wallet=True):

        if coins_to_burn and not isinstance(amt_to_burn, int):
            raise InvalidBurnAmount("Burn requested, but missing 'amt_to_burn' parameter")
        elif amt_to_burn and not coins_to_burn:
            raise InvalidBurnAmount("Burn requested, but missing 'coins_to_burn' parameter")

        # Step 1) Double check all input transactions have been added to wallet._slp_txo
        if require_tx_in_wallet:
            for txo in tx.inputs():
                addr = txo['address']
                prev_out = txo['prevout_hash']
                prev_n = txo['prevout_n']
                with wallet.lock:
                    try:
                        input_tx = wallet.transactions[prev_out]
                    except KeyError:
                        raise Exception('Wallet has not downloaded this transaction')
                    else:
                        try:
                            input_tx_slp_msg = slp.SlpMessage.parseSlpOutputScript(input_tx.outputs()[0][1])
                        except SlpInvalidOutputMessage:
                            pass
                        except SlpUnsupportedSlpTokenType:
                            raise UnsupportedSlpTokenType('Transaction contains an unsupported SLP' \
                                                            + ' input type')
                        else:
                            if input_tx_slp_msg.transaction_type == 'SEND':
                                if prev_n >= len(input_tx_slp_msg.op_return_fields['token_output']):
                                    continue
                                elif input_tx_slp_msg.op_return_fields['token_output'][prev_n] == 0:
                                    continue
                            elif input_tx_slp_msg.transaction_type in ['GENESIS', 'MINT']:
                                if input_tx_slp_msg.op_return_fields['mint_baton_vout'] and \
                                        prev_n not in [1, input_tx_slp_msg.op_return_fields['mint_baton_vout']]:
                                    continue
                                elif not input_tx_slp_msg.op_return_fields['mint_baton_vout'] and prev_n != 1:
                                    continue
                                elif input_tx_slp_msg.transaction_type == 'MINT' and \
                                        prev_n == 1 and \
                                        input_tx_slp_msg.op_return_fields['additional_token_quantity'] == 0:
                                    continue
                                elif input_tx_slp_msg.transaction_type == 'GENESIS' and \
                                        prev_n == 1 and \
                                        input_tx_slp_msg.op_return_fields['initial_token_mint_quantity'] == 0:
                                    continue
                            try:
                                with wallet.lock:
                                    assert wallet._slp_txo[addr][prev_out][prev_n]
                            except (KeyError, AssertionError):
                                raise SlpMissingInputRecord('Transaction contains an SLP input that is' \
                                                                + ' unknown to this wallet (missing from slp_txo).')

        # Step 2) Get SLP metadata in current transaction
        try:
            slp_msg = slp.SlpMessage.parseSlpOutputScript(tx.outputs()[0][1])
        except SlpParsingError:
            slp_msg = None

        # Step 3a) If non-SLP check for SLP inputs (only allow 
        #          spending slp inputs specified in 'coins_to_burn')
        if not slp_msg:
            for txo in tx.inputs():
                addr = txo['address']
                prev_out = txo['prevout_hash']
                prev_n = txo['prevout_n']
                slp_txo = None
                with wallet.lock:
                    try:
                        slp_txo = wallet._slp_txo[addr][prev_out][prev_n]
                    except KeyError:
                        pass
                if slp_txo:
                    is_burn_allowed = False
                    if coins_to_burn:
                        for c in coins_to_burn:
                            if c['prevout_hash'] == prev_out and c['prevout_n'] == prev_n:
                                is_burn_allowed = True
                                c['is_in_txn'] = True

                    if not is_burn_allowed:
                        print_error("SLP check failed for non-SLP transaction" \
                                        + " which contains SLP inputs.")
                        raise NonSlpTransactionHasSlpInputs('Transaction contains unspecified SLP inputs (Use the Burn Tool if you want to burn tokens).')

            # Check that all coins within 'coins_to_burn' are included in burn transaction
            if coins_to_burn:
                total_burn = 0
                for coin in coins_to_burn:
                    try:
                        if coin['is_in_txn'] and isinstance(coin['token_value'], int):
                            total_burn += coin['token_value']
                            continue
                    except KeyError:
                        raise MissingCoinToBeBurned('Transaction is missing required SLP inputs that were' \
                                                        + ' intended to be burned in this transaction.')
                    if total_burn != amt_to_burn:
                        print_error("Burn failed since specified burn amount does not match transaction")
                        raise InvalidBurnAmount('Burn failed since specified burn amount does not match transaction')

        # Step 3b) If SLP, check quantities and token id of inputs match output requirements
        elif slp_msg:
            if slp_msg.transaction_type == 'SEND':
                tid = slp_msg.op_return_fields['token_id_hex']
                # raise an Exception if:
                #   - [X] input quantity is greater than output quanitity (except if 'coins_to_burn')
                #   - [X] input quantity is less than output quanitity
                #   - [X] slp input does not match tokenId
                #   - [X] make sure outpoint is provided for every slp output and is P2PKH or P2SH
                #   - [ ] the proper token type is not respected in the output op_return message
                slp_outputs = slp_msg.op_return_fields['token_output']
                input_slp_qty = 0
                for txo in tx.inputs():
                    addr = txo['address']
                    prev_out = txo['prevout_hash']
                    prev_n = txo['prevout_n']
                    with wallet.lock:
                        try:
                            slp_input = wallet._slp_txo[addr][prev_out][prev_n]
                        except KeyError:
                            pass
                        else:
                            if slp_input['qty'] == 0:
                                continue
                            if isinstance(slp_input['qty'], int):
                                input_slp_qty += slp_input['qty']
                            if slp_input['token_id'] != tid:
                                print_error("SLP check failed for SEND due to incorrect" \
                                                + " tokenId in txn input")
                                raise SlpWrongTokenID('Transaction contains SLP inputs' \
                                                        + ' with incorrect token id.')

                if input_slp_qty < sum(slp_outputs):
                    print_error("SEND failed due to insufficient SLP inputs")
                    raise SlpInputsTooLow('Transaction SLP outputs exceed SLP inputs')
                elif coins_to_burn and input_slp_qty - amt_to_burn != sum(slp_outputs):
                    print_error("Burn failed since specified burn amount does not match transaction")
                    raise InvalidBurnAmount('Burn failed since specified burn amount does not match transaction')
                elif not coins_to_burn and input_slp_qty > sum(slp_outputs):
                    print_error("SEND failed due to SLP inputs too high")
                    raise SlpInputsTooHigh('Transaction SLP inputs exceed SLP outputs.')

                for i, out in enumerate(slp_msg.op_return_fields['token_output']):
                    try:
                        out = tx.outputs()[i]
                    except IndexError:
                        print_error("Transaction is missing vout for MINT operation" \
                                        + " token receiver")
                        raise MissingTokenReceiverOutpoint('Transaction is missing' \
                                                                + ' a required SLP output.')
                    else:
                        if i == 0:
                            assert out[0] == 2
                        elif out[1].kind not in [Address.ADDR_P2PKH, Address.ADDR_P2SH]:
                            print_error("Transaction token receiver vout is not P2PKH or P2SH")
                            raise BadSlpOutpointType('Tranaction SLP output must be p2pkh' \
                                                        + ' or p2sh output type.')
            elif slp_msg.transaction_type == 'MINT':
                tid = slp_msg.op_return_fields['token_id_hex']
                # raise an Exception if:
                #   - [X] Any non-baton SLP input is found
                #   - [X] Baton has wrong token ID
                #   - [ ] Minting transaction is being made from NFT child type baton
                for txo in tx.inputs():
                    addr = txo['address']
                    prev_out = txo['prevout_hash']
                    prev_n = txo['prevout_n']
                    with wallet.lock:
                        try:
                            slp_input = wallet._slp_txo[addr][prev_out][prev_n]
                        except KeyError:
                            pass
                        else:
                            if slp_input['qty'] == 0:
                                continue
                            if slp_input['qty'] != 'MINT_BATON':
                                print_error("Non-baton SLP input found in MINT")
                                raise SlpNonMintInput('MINT transaction contains non-baton SLP input.')
                            if slp_input['token_id'] != tid:
                                print_error("SLP check failed for MINT due to incorrect" \
                                                + " tokenId in baton")
                                raise SlpWrongTokenID('MINT transaction contains baton with incorrect' \
                                                        + ' token id.')
            elif slp_msg.transaction_type == 'GENESIS':
                # raise an Exception if:
                #   - [ ] NFT Child has quantity that is !== 1
                #   - [ ] NFT Child has minting baton vout specified
                #   - [ ] NFT Child does not grant exception for burning a coin in vin=0
                #   - [ ] NFT Child does not have a valid Type 0x81 coin in vin=0
                for txo in tx.inputs():
                    addr = txo['address']
                    prev_out = txo['prevout_hash']
                    prev_n = txo['prevout_n']
                    with wallet.lock:
                        try:
                            slp_input = wallet._slp_txo[addr][prev_out][prev_n]
                        except KeyError:
                            pass
                        else:
                            if slp_input['qty'] == 0:
                                continue
                            is_burn_allowed = False
                            if coins_to_burn:
                                for c in coins_to_burn:
                                    if c['prevout_hash'] == prev_out and c['prevout_n'] == prev_n:
                                        is_burn_allowed = True
                                        c['is_in_txn'] = True

                            if not is_burn_allowed:
                                print_error("SLP check failed for SLP GENESIS transaction" \
                                                                + " which contains SLP inputs.")
                                raise NonSlpTransactionHasSlpInputs('Genesis transaction contains unspecified SLP inputs.')

            if slp_msg.transaction_type in ['GENESIS', 'MINT']:
                # raise an Exception if:
                #   - [X] New baton outpoint is not P2PKH or P2SH type for Genesis or Mint
                #   - [X] Mint receiver has outpoint and is p2pkh or p2sh
                if slp_msg.op_return_fields['mint_baton_vout']:
                    try:
                        out = tx.outputs()[slp_msg.op_return_fields['mint_baton_vout']]
                    except IndexError:
                        print_error("Transaction is missing baton vout for MINT operation")
                        raise MissingMintBatonOutpoint('Transaction is missing baton' \
                                                            + ' vout for MINT operation')
                    else:
                        if out[1].kind not in [Address.ADDR_P2PKH, Address.ADDR_P2SH]:
                            print_error("Transaction baton receiver vout is not P2PKH or P2SH")
                            raise BadSlpOutpointType('Transaction baton receiver vout is not P2PKH' \
                                                        + ' or P2SH output type')

                    try:
                        out = tx.outputs()[1]
                    except IndexError:
                        print_error("Transaction is missing vout for MINT operation token receiver")
                        raise MissingTokenReceiverOutpoint('Transaction is missing vout for MINT' \
                                                                + ' operation token receiver')
                    else:
                        if out[1].kind not in [Address.ADDR_P2PKH, Address.ADDR_P2SH]:
                            print_error("Transaction token receiver vout is not P2PKH or P2SH")
                            raise BadSlpOutpointType('Transaction token receiver vout is not P2PKH' \
                                                        + ' or P2SH output type')

        # perform slp pre-flight check
        if networks.net.SLP_PREFLIGHT_CHECK:
            slp_preflight = SlpPreflightCheck.query(tx, selected_slp_coins=coins_to_burn, amt_to_burn=amt_to_burn)
            if not slp_preflight['ok']:
                raise Exception("slp pre-flight check failed: %s\n\n(node: %s)"%(slp_preflight.get('invalid_reason', json.dumps(slp_preflight)), slp_preflight['node']))

        # return True if this check passes
        print_error("Final SLP check passed")
        return True

# Exceptions caused by malformed or unexpected data found in parsing.
class SlpTransactionValidityError(Exception):
    pass

class SlpMissingInputRecord(SlpTransactionValidityError):
    pass

class NonSlpTransactionHasSlpInputs(SlpTransactionValidityError):
    # Cannot have SLP inputs in non-SLP transaction
    pass

class GenesisHasSlpInputs(SlpTransactionValidityError):
    # Genesis cannot have SLP inputs unless specified
    pass

class SlpWrongTokenID(SlpTransactionValidityError):
    # Wrong Token ID in input
    pass

class SlpInputsTooLow(SlpTransactionValidityError):
    # SLP input quantity too low in SEND transaction
    pass

class SlpInputsTooHigh(SlpTransactionValidityError):
    # SLP input quantity too high in SEND transaction
    pass

class MissingCoinToBeBurned(SlpTransactionValidityError):
    # SLP input quantity too high in SEND transaction
    pass

class InvalidBurnAmount(SlpTransactionValidityError):
    # SLP input quantity too high in SEND transaction
    pass

class SlpNonMintInput(SlpTransactionValidityError):
    # SLP MINT has non-baton SLP input
    pass

class MissingMintBatonOutpoint(SlpTransactionValidityError):
    # SLP MINT transaction missing baton outpoint
    pass

class MissingTokenReceiverOutpoint(Exception):
    # SLP transaction missing token receiver outpoint
    pass

class BadSlpOutpointType(Exception):
    # Outpoint not P2PKH or P2SH type
    pass

class UnsupportedSlpTokenType(Exception):
    # Input contains an unsupported SLP token type
    pass