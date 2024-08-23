from kiipy.aerial.client import LedgerClient, NetworkConfig

# connect to Kii test network using default parameters
ledger_client = LedgerClient(NetworkConfig.kii_testnet())

alice: str = '0xa92d504731aA3E99DF20ffd200ED03F9a55a6219'
balances = ledger_client.get_balance(alice)

# show all coin balances
print(balances)