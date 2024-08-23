import os
from send_tk import sendTk

w_to_send = os.getenv("WALLET2")
amount = 0.1

tx = sendTk().send(wallet_to_send=w_to_send, amount=amount)

print(f'Tx is: {tx}')
