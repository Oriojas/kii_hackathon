import os
from substrateinterface import SubstrateInterface, Keypair
from substrateinterface.exceptions import SubstrateRequestException

WALLET1 = os.getenv("WALLET1")
WALLET2 = os.getenv("WALLET2")
PASSPHRASE = os.getenv("PASSPHRASE")

substrate = SubstrateInterface(
    url="wss://mandala-rpc.aca-staging.network/ws",
    ss58_format=42
)

with open(f"{WALLET1}.json", 'r') as fp:
    json_data = fp.read()
    keypair = Keypair.create_from_encrypted_json(json_data, passphrase=PASSPHRASE, ss58_format=42)


call = substrate.compose_call(
    call_module='Balances',
    call_function='transfer',
    call_params={
        'dest': '5DnwSBic6fGC7nrxm514cXZ4tWKeqEpFBRK915LJeaGyWXWz',
        'value': 0.1 * 10 ** 12
    }
)

extrinsic = substrate.create_signed_extrinsic(call=call, keypair=keypair)

try:
    receipt = substrate.submit_extrinsic(extrinsic, wait_for_inclusion=True)
    print("Extrinsic '{}' sent and included in block '{}'".format(receipt.extrinsic_hash, receipt.block_hash))

except SubstrateRequestException as e:
    print("Failed to send: {}".format(e))
