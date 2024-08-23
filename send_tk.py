import os
from substrateinterface import SubstrateInterface, Keypair
from substrateinterface.exceptions import SubstrateRequestException

WALLET1 = os.getenv("WALLET1")
PASSPHRASE = os.getenv("PASSPHRASE")


class sendTk:
    """
    this class connect and send tokens to blockchain
    """

    def __init__(self):
        """
        this function connect to blockchain westend
        """

        try:
            self.substrate = SubstrateInterface(
                url="wss://mandala-rpc.aca-staging.network/ws",
                ss58_format=42
                )

            print("😀 last node running")

        except ConnectionRefusedError:
            print("⚠️ No local Substrate node running, try running 'start_local_substrate_node.sh' first")
            exit()

        print(f'🏃‍ Last node: {self.substrate.get_chain_head()}')

    def send(self, wallet_to_send, amount):
        """
        this function send tokens to blockchain wallet
        :param wallet_to_send: a wallet to send tokens
        :param amount: amount in ACA
        :return: transfer: is True if transaction OK, else False
        """

        with open(f"{WALLET1}.json", 'r') as fp:
            json_data = fp.read()
        keypair = Keypair.create_from_encrypted_json(json_data, passphrase=PASSPHRASE, ss58_format=42)

        call = self.substrate.compose_call(
            call_module='Balances',
            call_function='transfer',
            call_params={
                'dest': wallet_to_send,
                'value': amount * 10 ** 12
            }
        )

        try:
            extrinsic = self.substrate.create_signed_extrinsic(call=call, keypair=keypair, tip=1000000)
            receipt = self.substrate.submit_extrinsic(extrinsic, wait_for_inclusion=True)
            print(f"🤑 Extrinsic {receipt.extrinsic_hash} sent and included in block {receipt.block_hash}")
            transfer = True

        except SubstrateRequestException as e:
            print(f"😬 Failed to send: {format(e)}")
            transfer = False

        return transfer
