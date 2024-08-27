import os
from web3 import Web3
from dotenv import load_dotenv
from eth_account import Account
from kiipy.aerial.client import LedgerClient, NetworkConfig

load_dotenv()

MAIN_WALLET = os.getenv("MAIN_WALLET")
MAIN_WALLET_KEY = os.getenv("MAIN_WALLET_KEY")
PROVIDER_ENDPOINT = os.getenv("PROVIDER_ENDPOINT")


class SendTk:
    """
    A class used to send tokens to a specified wallet on the KII blockchain.

    Attributes
    ----------
    ledger : LedgerClient
        An instance of LedgerClient for interacting with the KII blockchain.
    web3 : Web3
        An instance of Web3 for interacting with the Ethereum blockchain.
    account : Account
        An instance of Account for signing transactions.

    Methods
    -------
    send(wallet_to_send: str, amount: float) -> str
        Sends a specified amount of tokens to the specified wallet.
        Returns the transaction hash.
    """

    def __init__(self):
        """
        Constructs all the necessary attributes for the SendTk class.
        """
        self.ledger = LedgerClient(NetworkConfig.kii_testnet())
        self.web3 = Web3(Web3.HTTPProvider(PROVIDER_ENDPOINT))

        if self.web3.is_connected():
            print("Connected to the KII blockchain")
        else:
            print("Failed to connect to the blockchain")

        self.account = Account.from_key(MAIN_WALLET_KEY)

    def send(self, wallet_to_send: str, amount: float) -> str:
        """
        Sends a specified amount of tokens to the specified wallet.

        Parameters
        ----------
        wallet_to_send : str
            The address of the wallet to send tokens to.
        amount : float
            The amount of tokens to send.

        Returns
        -------
        str
            The transaction hash.
        """
        print(f"Sender   Address: {MAIN_WALLET} "
              f"Balance: {self.ledger.get_balance(MAIN_WALLET)}")

        print(f"Receiver Address: {wallet_to_send} "
              f"Balance: {self.ledger.get_balance(wallet_to_send)}")

        nonce = self.web3.eth.get_transaction_count(self.account.address)

        transaction = {
            'to': wallet_to_send,
            'value': self.web3.to_wei(amount, 'ether'),
            'gas': 21000,
            'gasPrice': self.web3.to_wei(50, 'gwei'),
            'nonce': nonce,
            'chainId': self.web3.eth.chain_id
        }

        signed_txn = self.account.sign_transaction(transaction)

        tx_hash = self.web3.eth.send_raw_transaction(signed_txn.rawTransaction)

        print(f'Transaction sent with hash: {self.web3.to_hex(tx_hash)}')

        receipt = self.web3.eth.wait_for_transaction_receipt(tx_hash)

        print(f'Transaction {self.web3.to_hex(tx_hash)} has been confirmed in block {receipt.blockNumber}')

        print(f"Sender   Address: {MAIN_WALLET} "
              f"Balance: {self.ledger.get_balance(MAIN_WALLET)}")

        print(f"Receiver Address: {wallet_to_send} "
              f"Balance: {self.ledger.get_balance(wallet_to_send)}")

        return self.web3.to_hex(tx_hash)


if __name__ == "__main__":
    send_tk = SendTk()
    tx_hash = send_tk.send(wallet_to_send='0x1d870f1210e66cba98093682b84d4491Ec04141b', amount=3)
    print(f'Transaction hash (readable): {tx_hash}')
