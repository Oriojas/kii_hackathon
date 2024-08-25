import os
from send_tk import SendTk

w_to_send = os.getenv("WALLET2")
amount = 0.1

tx = SendTk().send(wallet_to_send=w_to_send, amount=amount)

print(f'Tx is: {tx}')
