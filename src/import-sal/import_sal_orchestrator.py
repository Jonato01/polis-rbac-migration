#!/usr/bin/env python3
"""
Import SAL Orchestrator v5 - Core orchestration and coordination
Gestisce il coordinamento dei processi di import SAL
"""
from azure_clients import get_blob_service_client, get_servicebus_client, describe_auth_mode
import os
import sys
import json
import time
import logging
import threading
from datetime import datetime
from azure.servicebus import ServiceBusClient, ServiceBusReceiveMode

from import_sal_service_manager import ImportSalServiceManager
from import_sal_utils import ImportSalUtils

import os
from dotenv import load_dotenv

# Carica il file .env
load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(threadName)s] - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

class ImportSalOrchestrator:
    def __init__(self):
        # Configuration - NO DEFAULTS
        self.SERVICE_BUS_CONN = os.getenv('SERVICE_BUS_CONNECTION_STRING')
        self.POLIS_API_DATABASE_URL = os.getenv('POLIS_API_DATABASE_URL')
        self.AZURE_STORAGE = os.getenv('AZURE_STORAGE_API_URL')
        self.SUBSCRIPTION_ID = os.getenv('AZURE_SUBSCRIPTION_ID')
        self.RESOURCE_GROUP = os.getenv('AZURE_RESOURCE_GROUP')
        self.LOCATION = os.getenv('AZURE_LOCATION')
        
        # Service Bus Queues Configuration
        self.IMPORT_QUEUE = os.getenv('IMPORT_QUEUE')
        self.ROLLBACK_QUEUE = os.getenv('ROLLBACK_QUEUE')
        self.RESPONSE_QUEUE = os.getenv('RESPONSE_QUEUE')
        
        # Container Apps Service Queues
        self.SFTP_SERVICE_QUEUE = os.getenv('SFTP_SERVICE_QUEUE', 'import-sal-sftp')
        self.DB_BACKUP_SERVICE_QUEUE = os.getenv('DB_BACKUP_SERVICE_QUEUE', 'import-sal-db-backup')
        self.CONTAINER_BACKUP_SERVICE_QUEUE = os.getenv('CONTAINER_BACKUP_SERVICE_QUEUE', 'import-sal-container-backup')
        self.DOCUMENTATION_SERVICE_QUEUE = os.getenv('DOCUMENTATION_SERVICE_QUEUE', 'import-sal-documentation')
        self.PERSONALE_SERVICE_QUEUE = os.getenv('PERSONALE_SERVICE_QUEUE', 'import-sal-personale')
        self.FATTURE_SERVICE_QUEUE = os.getenv('FATTURE_SERVICE_QUEUE', 'import-sal-fatture')
        self.DB_ROLLBACK_SERVICE_QUEUE = os.getenv('DB_ROLLBACK_SERVICE_QUEUE', 'import-sal-db-rollback')
        self.DB_ROLLBACK_SAL_SERVICE_QUEUE = os.getenv('DB_ROLLBACK_SAL_SERVICE_QUEUE', 'import-sal-db-rollback-sal')
        self.STORAGE_CLEANUP_SERVICE_QUEUE = os.getenv('STORAGE_CLEANUP_SERVICE_QUEUE', 'import-sal-storage-cleanup')

        # Storage Configuration
        self.AZURE_STORAGE_CONNECTION_STRING = os.getenv('AZURE_STORAGE_CONNECTION_STRING')
        
        # SFTP Configuration  
        self.SFTP_CONFIG = {
            'host': os.getenv('SFTP_HOST'),
            'port': os.getenv('SFTP_PORT'),
            'user': os.getenv('SFTP_USER'),
            'password': os.getenv('SFTP_PASSWORD'),
            'base_path': os.getenv('SFTP_BASE_PATH')
        }
        
        # Storage Configuration
        self.STORAGE_CONTAINER = os.getenv('STORAGE_CONTAINER')
        self.RESPONSE_QUEUE_NAME = os.getenv('RESPONSE_QUEUE')
        
        # Notification Configuration
        self.NOTIFICATION_USER_ID = int(os.getenv('NOTIFICATION_USER_ID')) if os.getenv('NOTIFICATION_USER_ID') else None
        self.NOTIFICATION_ENDPOINT = os.getenv('NOTIFICATION_ENDPOINT')
        
        # Database configuration
        self.db_config = {
            'host': os.getenv('DB_HOST'),
            'user': os.getenv('DB_USER'),
            'password': os.getenv('DB_PASSWORD'),
            'database': os.getenv('DB_DATABASE')
        }

        # Track active imports with backup paths
        self.active_imports = {}
        
        # Store backup paths for each SAL
        self.db_backup_paths = {}
        self.container_backup_paths = {}
        
        # Track active rollbacks
        self.active_rollbacks = {}
        
        # Initialize service manager and utils with shared config
        self.service_manager = ImportSalServiceManager(self)
        self.utils = ImportSalUtils(self)
        
        # Validate required configuration
        self._validate_configuration()
        
        logger.info("="*60)
        logger.info("IMPORT SAL ORCHESTRATOR v5 - WITH CONTAINER BACKUP")
        logger.info("="*60)
        logger.info(f"Subscription: {self.SUBSCRIPTION_ID}")
        logger.info(f"Resource Group: {self.RESOURCE_GROUP}")
        logger.info(f"Location: {self.LOCATION}")
        logger.info("="*60)
    
    def _validate_configuration(self):
        """Validate all required environment variables"""
        required_vars = {
            'SERVICE_BUS_NAMESPACE|SERVICE_BUS_CONNECTION_STRING': (os.getenv('SERVICE_BUS_NAMESPACE') or self.SERVICE_BUS_CONN),
            'POLIS_API_DATABASE_URL': self.POLIS_API_DATABASE_URL,
            'AZURE_STORAGE_API_URL': self.AZURE_STORAGE,
            'AZURE_SUBSCRIPTION_ID': self.SUBSCRIPTION_ID,
            'AZURE_RESOURCE_GROUP': self.RESOURCE_GROUP,
            'AZURE_LOCATION': self.LOCATION,
            'IMPORT_QUEUE': self.IMPORT_QUEUE,
            'ROLLBACK_QUEUE': self.ROLLBACK_QUEUE,
            'RESPONSE_QUEUE': self.RESPONSE_QUEUE,
            'STORAGE_CONTAINER': self.STORAGE_CONTAINER,
            'NOTIFICATION_USER_ID': self.NOTIFICATION_USER_ID,
            'NOTIFICATION_ENDPOINT': self.NOTIFICATION_ENDPOINT,
            'DB_HOST': self.db_config['host'],
            'DB_USER': self.db_config['user'],
            'DB_PASSWORD': self.db_config['password'],
            'DB_DATABASE': self.db_config['database']
        }
        
        # Add SFTP config validation
        for key, value in self.SFTP_CONFIG.items():
            required_vars[f'SFTP_{key.upper()}'] = value

        missing_vars = [var for var, value in required_vars.items() if value is None]
        if missing_vars:
            logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
            sys.exit(1)
    
    def listen_for_new_imports(self):
        """Listen for new SAL imports on import queue"""
        logger.info(f"[IMPORT LISTENER] Starting - listening on '{self.IMPORT_QUEUE}' queue...")
        
        from azure.servicebus.exceptions import ServiceBusError

        while True:  # Loop esterno per riconnessione automatica
            try:
                client = get_servicebus_client()
                logger.info(f"[IMPORT LISTENER] Service Bus client created successfully")

                with client:
                    logger.info(f"[IMPORT LISTENER] Connected to Service Bus")
                    receiver = client.get_queue_receiver(
                        queue_name=self.IMPORT_QUEUE,
                        receive_mode=ServiceBusReceiveMode.RECEIVE_AND_DELETE,
                        max_wait_time=60  # Timeout di 60 secondi invece di infinito
                    )
                    logger.info(f"[IMPORT LISTENER] Queue receiver created for '{self.IMPORT_QUEUE}'")

                    with receiver:
                        logger.info(f"[IMPORT LISTENER] ✅ Now listening for messages on '{self.IMPORT_QUEUE}'...")

                        for message in receiver:
                            try:
                                logger.info(f"[IMPORT LISTENER] 📨 MESSAGE RECEIVED!")
                                logger.info(f"[IMPORT LISTENER] Message ID: {getattr(message, 'message_id', 'n/a')}")
                                logger.info(f"[IMPORT LISTENER] Message content: {str(message)}")

                                data = json.loads(str(message))
                                sal_name = data.get('sal_storage_name')

                                logger.info("="*60)
                                logger.info(f"[IMPORT] NEW SAL: {sal_name}")
                                logger.info(f"[IMPORT] Full data: {data}")
                                logger.info("="*60)

                                # Initialize tracking
                                import uuid
                                request_id = str(uuid.uuid4())

                                self.active_imports[sal_name] = {
                                    'current_step': 'sftp',
                                    'status': 'running',
                                    'data': data,
                                    'request_id': request_id,
                                    'db_backup_path': None,
                                    'container_backup_paths': None,
                                    'start_time': datetime.now().isoformat()
                                }

                                logger.info(f"[IMPORT] Request ID: {request_id}")
                                logger.info(f"[IMPORT] Triggering SFTP service...")

                                # Step 1: SFTP Copy (using Container Apps)
                                self.service_manager.trigger_sftp_service(data)

                                logger.info(f"[IMPORT] SFTP service triggered")

                            except Exception as e:
                                logger.error(f"[IMPORT] ❌ Error processing message: {e}")
                                logger.error(f"[IMPORT] Message content: {str(message)}")
                                import traceback
                                logger.error(f"[IMPORT] Full traceback: {traceback.format_exc()}")
                                # Message already removed in RECEIVE_AND_DELETE mode

            except ServiceBusError as e:
                # Timeout normale o errore AMQP - riconnetti automaticamente
                logger.info(f"[IMPORT LISTENER] Service Bus error occurred: {e}")
                logger.info(f"[IMPORT LISTENER] Reconnecting to Service Bus...")
                time.sleep(2)  # Breve pausa prima di riconnettere
                continue

            except Exception as e:
                logger.error(f"[IMPORT LISTENER] ❌ Fatal error in listener: {e}")
                import traceback
                logger.error(f"[IMPORT LISTENER] Full traceback: {traceback.format_exc()}")
                logger.info(f"[IMPORT LISTENER] Attempting to reconnect in 5 seconds...")
                time.sleep(5)
                continue  # Riprova anche su errori critici

    def listen_for_rollback_requests(self):
        """Listen for SAL rollback requests on rollback queue"""
        logger.info(f"[ROLLBACK LISTENER] Starting - listening on '{self.ROLLBACK_QUEUE}' queue...")
        
        from azure.servicebus.exceptions import ServiceBusError

        while True:  # Loop esterno per riconnessione automatica
            try:
                client = get_servicebus_client()
                with client:
                    receiver = client.get_queue_receiver(
                        queue_name=self.ROLLBACK_QUEUE,
                        receive_mode=ServiceBusReceiveMode.RECEIVE_AND_DELETE,
                        max_wait_time=60  # Timeout di 60 secondi invece di infinito
                    )
                    with receiver:
                        for message in receiver:
                            try:
                                data = json.loads(str(message))
                                sal_info = data.get('sal_to_delete', {})
                                sal_name = sal_info.get('sal_name')
                                sal_number = sal_info.get('sal_number')
                                process_id = data.get('process_id')

                                logger.info("="*60)
                                logger.info(f"[ROLLBACK] REQUEST FOR SAL: {sal_name} (#{sal_number})")
                                logger.info(f"[ROLLBACK] Process ID: {process_id}")
                                logger.info(f"[ROLLBACK] Requested by: {data.get('requested_by', 'unknown')}")
                                logger.info("="*60)

                                # Initialize tracking for rollback
                                self.active_rollbacks[sal_name] = {
                                    'process_id': process_id,
                                    'sal_number': sal_number,
                                    'status': 'running',
                                    'data': data,
                                    'start_time': datetime.now().isoformat()
                                }

                                # Launch DB deletion service
                                logger.info(f"[ROLLBACK] Launching DB deletion service for SAL {sal_number}...")
                                self.service_manager.launch_db_rollback_sal(sal_name, sal_number)

                            except Exception as e:
                                logger.error(f"[ROLLBACK] Error: {e}")
                                # Message already removed in RECEIVE_AND_DELETE mode

            except ServiceBusError as e:
                # Timeout normale o errore AMQP - riconnetti automaticamente
                logger.info(f"[ROLLBACK LISTENER] Service Bus error occurred: {e}")
                logger.info(f"[ROLLBACK LISTENER] Reconnecting to Service Bus...")
                time.sleep(2)  # Breve pausa prima di riconnettere
                continue

            except Exception as e:
                logger.error(f"[ROLLBACK LISTENER] ❌ Fatal error in rollback listener: {e}")
                import traceback
                logger.error(f"[ROLLBACK LISTENER] Full traceback: {traceback.format_exc()}")
                logger.info(f"[ROLLBACK LISTENER] Attempting to reconnect in 5 seconds...")
                time.sleep(5)
                continue  # Riprova anche su errori critici

    def listen_for_responses(self):
        """Listen for service responses on response queue with automatic reconnection on timeout"""
        logger.info(f"[RESPONSE LISTENER] Starting - listening on '{self.RESPONSE_QUEUE}' queue...")
        
        from azure.servicebus.exceptions import ServiceBusError

        while True:  # Loop esterno per riconnessione automatica
            try:
                client = get_servicebus_client()
                logger.info(f"[RESPONSE LISTENER] Service Bus client created successfully")

                with client:
                    logger.info(f"[RESPONSE LISTENER] Connected to Service Bus")
                    receiver = client.get_queue_receiver(
                        queue_name=self.RESPONSE_QUEUE,
                        receive_mode=ServiceBusReceiveMode.RECEIVE_AND_DELETE,
                        max_wait_time=60  # Timeout di 60 secondi invece di infinito
                    )
                    logger.info(f"[RESPONSE LISTENER] Queue receiver created for '{self.RESPONSE_QUEUE}'")

                    with receiver:
                        logger.info(f"[RESPONSE LISTENER] ✅ Now listening for messages on '{self.RESPONSE_QUEUE}'...")

                        for message in receiver:
                            try:
                                logger.info(f"[RESPONSE LISTENER] 📨 MESSAGE RECEIVED!")
                                logger.info(f"[RESPONSE LISTENER] Message ID: {getattr(message, 'message_id', 'n/a')}")
                                logger.info(f"[RESPONSE LISTENER] Message content: {str(message)}")

                                response = json.loads(str(message))
                                task = response.get('task')
                                sal = response.get('sal')
                                status = response.get('status')

                                logger.info("="*60)
                                logger.info(f"[RESPONSE] Task: {task}, SAL: {sal}, Status: {status}")
                                logger.info("="*60)

                                # Delegate to service manager for handling
                                self.service_manager.handle_service_response(task, sal, status, response)

                                logger.info(f"[RESPONSE] ✅ Message handled")

                            except Exception as e:
                                logger.error(f"[RESPONSE] ❌ Error processing message: {e}")
                                logger.error(f"[RESPONSE] Message content: {str(message)}")
                                import traceback
                                logger.error(f"[RESPONSE] Full traceback: {traceback.format_exc()}")
                                # Message already removed in RECEIVE_AND_DELETE mode

            except ServiceBusError as e:
                # Timeout normale o errore AMQP - riconnetti automaticamente
                logger.info(f"[RESPONSE LISTENER] Service Bus error occurred: {e}")
                logger.info(f"[RESPONSE LISTENER] Reconnecting to Service Bus...")
                time.sleep(2)  # Breve pausa prima di riconnettere
                continue

            except Exception as e:
                logger.error(f"[RESPONSE LISTENER] ❌ Fatal error in listener: {e}")
                import traceback
                logger.error(f"[RESPONSE LISTENER] Full traceback: {traceback.format_exc()}")
                logger.info(f"[RESPONSE LISTENER] Attempting to reconnect in 5 seconds...")
                time.sleep(5)
                continue  # Riprova anche su errori critici

    def start(self):
        """Start all listeners in separate threads"""
        logger.info("Starting Import SAL orchestrator v5...")
        
        # Thread for new imports
        import_thread = threading.Thread(
            target=self.listen_for_new_imports,
            name="ImportListener"
        )
        import_thread.daemon = True
        import_thread.start()
        
        # Thread for rollback requests
        rollback_thread = threading.Thread(
            target=self.listen_for_rollback_requests,
            name="RollbackListener"
        )
        rollback_thread.daemon = True
        rollback_thread.start()
        
        # Thread for responses
        response_thread = threading.Thread(
            target=self.listen_for_responses,
            name="ResponseListener"
        )
        response_thread.daemon = True
        response_thread.start()
        
        # Start web server
        self.utils.start_web_server()

        logger.info("All listeners and web server started. Waiting for messages...")

        # Keep main thread alive
        try:
            while True:
                time.sleep(10)
                # Status logging
                if self.active_imports:
                    logger.info(f"[STATUS] Active imports: {list(self.active_imports.keys())}")
                if self.active_rollbacks:
                    logger.info(f"[STATUS] Active rollbacks: {list(self.active_rollbacks.keys())}")
                if self.db_backup_paths:
                    logger.info(f"[STATUS] DB backup paths: {len(self.db_backup_paths)} SALs")
                if self.container_backup_paths:
                    logger.info(f"[STATUS] Container backup paths: {len(self.container_backup_paths)} SALs")
        except KeyboardInterrupt:
            logger.info("Shutting down...")

def main():
    try:
        orchestrator = ImportSalOrchestrator()
        orchestrator.start()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()