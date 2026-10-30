#!/usr/bin/env python3
"""
DB Rollback SAL Service - Cancella tutti i dati di un SAL specifico dal database
ATTENZIONE: Servizio solo per ambiente di TEST - NON usare in produzione
"""
from azure_clients import get_blob_service_client, get_servicebus_client, describe_auth_mode
import os
import sys
import json
import time
import logging
import mysql.connector
from datetime import datetime
from azure.servicebus import ServiceBusClient, ServiceBusMessage, ServiceBusReceiveMode

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

def send_completion_message(service_bus_conn, sal_name, sal_number, success, details, queue_name, rows_deleted=None):
    """Send message to Service Bus with retries"""
    message_data = {
        'task': 'db-rollback-sal',
        'sal': sal_name,
        'sal_number': sal_number,
        'status': 'success' if success else 'failed',
        'details': details,
        'rows_deleted': rows_deleted,
        'timestamp': datetime.now().isoformat()
    }
    
    for attempt in range(5):
        try:
            logger.info(f"Sending message (attempt {attempt + 1}/5)...")
            
            client = get_servicebus_client()
            with client:
                sender = client.get_queue_sender(queue_name=queue_name)
                with sender:
                    message = ServiceBusMessage(json.dumps(message_data))
                    sender.send_messages(message)
            
            time.sleep(2)
            logger.info("✅ Message sent successfully!")
            return True
        except Exception as e:
            logger.error(f"Attempt {attempt + 1} failed: {e}")
            if attempt < 4:
                time.sleep(2 ** attempt)
    
    logger.error("Failed to send message after all retries")
    return False


def delete_sal_data(sal_number, db_config):
    """Elimina tutti i dati di un SAL dal database"""

    rows_deleted = {}
    total_deleted = 0

    # Query di cancellazione in ordine per rispettare le FK
    delete_queries = [
        ("costo_personale",
         f"DELETE FROM costo_personale WHERE id_sal = (SELECT id_sal FROM sal WHERE numero_sal = {sal_number})"),
        ("costo_medio_unitario_sal",
         f"DELETE FROM costo_medio_unitario_sal WHERE id_sal = (SELECT id_sal FROM sal WHERE numero_sal = {sal_number})"),
        ("pagamenti", f"DELETE FROM pagamenti WHERE id_sal = (SELECT id_sal FROM sal WHERE numero_sal = {sal_number})"),
        ("dettagli_fattura",
         f"DELETE FROM dettagli_fattura WHERE id_sal = (SELECT id_sal FROM sal WHERE numero_sal = {sal_number})"),
        ("import_sessions",
         f"DELETE FROM import_sessions WHERE id_sal = (SELECT id_sal FROM sal WHERE numero_sal = {sal_number})"),
        ("sal", f"DELETE FROM sal WHERE numero_sal = {sal_number}")
    ]

    connection = None
    cursor = None

    try:
        # Connessione al database
        logger.info("Connecting to database...")
        connection = mysql.connector.connect(
            host=db_config['host'],
            user=db_config['user'],
            password=db_config['password'],
            database=db_config['database']
        )
        cursor = connection.cursor()

        # Disabilita autocommit per gestire manualmente la transazione
        connection.autocommit = False

        # Verifica che il SAL esista
        cursor.execute(f"SELECT id_sal FROM sal WHERE numero_sal = {sal_number}")
        result = cursor.fetchone()

        if not result:
            logger.error(f"SAL numero {sal_number} not found in database")
            return False, rows_deleted, f"SAL {sal_number} not found"

        id_sal = result[0]
        logger.info(f"Found SAL {sal_number} with id_sal = {id_sal}")

        logger.info("Starting deletion...")

        # Esegui cancellazioni
        for table_name, query in delete_queries:
            try:
                logger.info(f"Deleting from {table_name}...")
                cursor.execute(query)
                affected_rows = cursor.rowcount
                rows_deleted[table_name] = affected_rows
                total_deleted += affected_rows

                if affected_rows > 0:
                    logger.info(f"  ✓ Deleted {affected_rows} rows from {table_name}")
                else:
                    logger.info(f"  - No rows to delete from {table_name}")

            except mysql.connector.Error as e:
                logger.error(f"  ✗ Error deleting from {table_name}: {e}")
                connection.rollback()
                return False, rows_deleted, f"Failed at {table_name}: {str(e)}"

        # Commit transazione
        connection.commit()
        logger.info("✅ Transaction committed successfully")

        # Report finale
        logger.info("=" * 60)
        logger.info("DELETION SUMMARY:")
        for table, count in rows_deleted.items():
            if count > 0:
                logger.info(f"  {table}: {count} rows deleted")
        logger.info(f"  TOTAL: {total_deleted} rows deleted")
        logger.info("=" * 60)

        return True, rows_deleted, f"Successfully deleted SAL {sal_number}"

    except mysql.connector.Error as e:
        logger.error(f"Database error: {e}")
        if connection:
            connection.rollback()
        return False, rows_deleted, f"Database error: {str(e)}"

    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        if connection:
            connection.rollback()
        return False, rows_deleted, f"Unexpected error: {str(e)}"

    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()
        logger.info("Database connection closed")

def process_rollback_request(message_data):
    """Process a single rollback request"""
    # Extract data from message
    sal_storage_name = message_data.get('sal_storage_name')
    sal_number = message_data.get('sal_number')
    request_id = message_data.get('request_id', 'unknown')

    # Get environment variables
    SERVICE_BUS = os.getenv('SERVICE_BUS_CONNECTION_STRING')
    
    # Database configuration
    db_config = {
        'host': os.getenv('DB_HOST', 'colibri-mysql-flexible.mysql.database.azure.com'),
        'user': os.getenv('DB_USER', 'polisammin'),
        'password': os.getenv('DB_PASSWORD', 'ComplexPass123!'),
        'database': os.getenv('DB_DATABASE', 'polis')
    }
    
    # Service Bus Configuration
    RESPONSE_QUEUE = os.getenv('RESPONSE_QUEUE', 'orchestrator-responses')

    logger.info("="*60)
    logger.info("DB ROLLBACK SAL SERVICE - TEST ONLY")
    logger.info("="*60)
    logger.info(f"[{request_id}] SAL Storage: {sal_storage_name}")
    logger.info(f"[{request_id}] SAL Number: {sal_number}")
    logger.info(f"[{request_id}] Database: {db_config['database']}")
    logger.info(f"[{request_id}] Container: {os.getenv('HOSTNAME', 'unknown')}")
    logger.info("="*60)
    
    # Validate required environment variables
    required_vars = {
        'SERVICE_BUS_NAMESPACE|SERVICE_BUS_CONNECTION_STRING': (os.getenv('SERVICE_BUS_NAMESPACE') or SERVICE_BUS),
        'DB_HOST': db_config['host'],
        'DB_USER': db_config['user'],
        'DB_PASSWORD': db_config['password'],
        'DB_DATABASE': db_config['database'],
        'RESPONSE_QUEUE': RESPONSE_QUEUE
    }

    missing_vars = [var for var, value in required_vars.items() if value is None]
    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        raise Exception(f"Missing environment variables: {', '.join(missing_vars)}")

    if not sal_storage_name:
        raise Exception("sal_storage_name not provided in message")

    if not sal_number:
        raise Exception("sal_number not provided in message")

    try:
        sal_number = int(sal_number)
    except ValueError:
        logger.error(f"Invalid SAL_NUMBER: {sal_number} (must be an integer)")
        raise Exception(f"Invalid SAL_NUMBER: {sal_number} (must be an integer)")

    try:
        # Conferma operazione
        logger.warning(f"[{request_id}] ⚠️  WARNING: This will DELETE all data for SAL {sal_number}")
        logger.warning(f"[{request_id}] ⚠️  This operation is IRREVERSIBLE!")
        logger.info(f"[{request_id}] Starting deletion in 5 seconds...")
        time.sleep(5)

        # Esegui cancellazione
        logger.info(f"[{request_id}] Starting SAL data deletion...")
        success, rows_deleted, message = delete_sal_data(sal_number, db_config)
        
        if success:
            logger.info("="*60)
            logger.info(f"[{request_id}] ✅ SAL DATA DELETION COMPLETED SUCCESSFULLY")
            logger.info(f"[{request_id}]    SAL Number: {sal_number}")
            logger.info(f"[{request_id}]    Total rows deleted: {sum(rows_deleted.values())}")
            logger.info("="*60)
            
            # Send success message
            send_completion_message(
                SERVICE_BUS, sal_storage_name, sal_number, True,
                message, RESPONSE_QUEUE, rows_deleted
            )
        else:
            logger.error(f"[{request_id}] ❌ SAL data deletion failed")
            send_completion_message(
                SERVICE_BUS, sal_storage_name, sal_number, False,
                message, RESPONSE_QUEUE, rows_deleted
            )
            raise Exception(f"SAL data deletion failed: {message}")

        # Wait before completing
        logger.info(f"[{request_id}] Waiting 5 seconds...")
        time.sleep(5)

        logger.info(f"[{request_id}] ✅ Message completed")

    except Exception as e:
        logger.error(f"[{request_id}] ERROR: {e}")
        send_completion_message(SERVICE_BUS, sal_storage_name, sal_number, False, str(e), RESPONSE_QUEUE, {})
        raise


def listen_for_rollback_requests():
    """Listen for rollback requests on the queue"""
    SERVICE_BUS_CONN = os.getenv('SERVICE_BUS_CONNECTION_STRING')
    REQUEST_QUEUE = os.getenv('DB_ROLLBACK_SAL_SERVICE_QUEUE', 'import-sal-db-rollback-sal')

    logger.info("🚀 Starting queue processor...")
    logger.info(f"📡 Listening for messages on queue: {REQUEST_QUEUE}")

    if describe_auth_mode('SERVICE_BUS_NAMESPACE', 'SERVICE_BUS_CONNECTION_STRING') == 'missing':
        logger.error("SERVICE_BUS_CONNECTION_STRING not configured")
        sys.exit(1)

    try:
        client = get_servicebus_client()
        with client:
            receiver = client.get_queue_receiver(queue_name=REQUEST_QUEUE, receive_mode=ServiceBusReceiveMode.RECEIVE_AND_DELETE)
            with receiver:
                logger.info(f"✅ Connected to queue: {REQUEST_QUEUE}")

                for message in receiver:
                    try:
                        logger.info("📨 Received message: " + str(message))

                        # Parse message
                        message_data = json.loads(str(message))

                        # Process the rollback request
                        process_rollback_request(message_data)

                        # Message auto-deleted (RECEIVE_AND_DELETE)

                    except Exception as e:
                        logger.error(f"❌ Error processing message: {e}")
                        import traceback
                        logger.error(f"Full traceback: {traceback.format_exc()}")
                        # Message already removed in RECEIVE_AND_DELETE

    except Exception as e:
        logger.error(f"❌ Fatal error in queue listener: {e}")
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")
        sys.exit(1)


def main():
    """Main function"""
    try:
        listen_for_rollback_requests()
    except KeyboardInterrupt:
        logger.info("🛑 Shutting down...")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()