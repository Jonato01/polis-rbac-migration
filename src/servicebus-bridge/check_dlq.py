"""
Tool manuale: legge i primi messaggi della dead letter queue di una coda.

Uso: SERVICE_BUS_NAMESPACE=<ns> python check_dlq.py [nome_coda]
Autenticazione tramite azure_clients (managed identity / az login; connection string legacy
solo via SERVICE_BUS_CONNECTION_STRING).
"""
import sys

from azure_clients import get_servicebus_client

queue_name = sys.argv[1] if len(sys.argv) > 1 else "campionamento"
client = get_servicebus_client()

print(f"=== LETTURA DEAD LETTER QUEUE: {queue_name} ===\n")
with client.get_queue_receiver(queue_name, sub_queue="deadletter", max_wait_time=5) as receiver:
    messages = receiver.receive_messages(max_message_count=2, max_wait_time=5)

    for i, msg in enumerate(messages, 1):
        print(f"MESSAGGIO {i}:")
        print(f"  Body: {str(msg)[:300]}")
        print(f"  Dead Letter Reason: {msg.dead_letter_reason}")
        print(f"  Dead Letter Description: {msg.dead_letter_error_description}")
        print()
