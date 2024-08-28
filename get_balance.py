from kiipy.aerial.client import LedgerClient, NetworkConfig

class GetBalance:
    """
    A class used to interact with a blockchain ledger to get a wallet's balance.

    Attributes
    ----------
    ledger_client : LedgerClient
        An instance of LedgerClient for interacting with the blockchain ledger.

    Methods
    -------
    __init__()
        Initializes the getBalance class and sets up the ledger_client attribute.

    fit(wallet)
        Retrieves the balance of a given wallet.

    """

    def __init__(self):
        """
        Initializes the getBalance class and sets up the ledger_client attribute.

        Tries to establish a connection to the blockchain ledger using LedgerClient and NetworkConfig.
        If a ConnectionRefusedError occurs, it prints a warning message and exits the program.

        """
        try:
            self.ledger_client = LedgerClient(NetworkConfig.kii_testnet())
            print("😀 chain running")
        except ConnectionRefusedError:
            print("⚠️ No connection blockchain")
            exit()

    def fit(self, wallet):
        """
        Retrieves the balance of a given wallet.

        Parameters
        ----------
        wallet : str
            The wallet address for which to retrieve the balance.

        Returns
        -------
        balance_off : int
            The balance of the given wallet.

        """
        balance_off = self.ledger_client.get_balance(wallet) / 1000000000000000000
        print(balance_off)
        return balance_off

if __name__ == '__main__':
    balance_obj = GetBalance()
    balance_off = balance_obj.fit(wallet='0x1d870f1210e66cba98093682b84d4491Ec04141b')
    print(f'Balance: {balance_off}')
