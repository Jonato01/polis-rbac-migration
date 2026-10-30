#!/usr/bin/env python3
"""
Import SAL Utils - Utility functions shared across the orchestrator
Gestisce notifiche, container management, e funzioni di supporto
"""
from azure_clients import get_blob_service_client, get_servicebus_client, describe_auth_mode
import os
import logging
import threading

logger = logging.getLogger(__name__)

class ImportSalUtils:
    def __init__(self, orchestrator):
        """Initialize with reference to main orchestrator"""
        self.orchestrator = orchestrator
    
    def send_notification(self, sal, tipo, messaggio):
        """Invia notifica all'API"""
        try:
            import requests
            
            # Estrai sal_id dalla stringa (es: "9_SAL_31_08_2025" -> 9)
            sal_id = None
            if sal and '_' in sal:
                sal_number = sal.split('_')[0]
                if sal_number.isdigit():
                    sal_id = int(sal_number)
            
            # Costruisci payload
            payload = {
                "messaggio": messaggio,
                "tipo": tipo,
                "user_id": self.orchestrator.NOTIFICATION_USER_ID
            }
            
            if sal_id is not None:
                payload["sal_id"] = sal_id

            url = f"{self.orchestrator.POLIS_API_DATABASE_URL}/{self.orchestrator.NOTIFICATION_ENDPOINT}"
            headers = {
                "accept": "application/json",
                "Content-Type": "application/json"
            }

            response = requests.post(url, json=payload, headers=headers)

            if response.status_code == 200:
                logger.info(f"[NOTIFICATION] Sent: {messaggio}")
            else:
                logger.warning(f"[NOTIFICATION] Failed: {response.status_code}")

        except Exception as e:
            logger.error(f"[NOTIFICATION] Error: {e}")

    def send_service_message(self, queue_name, message_data):
        """Send message to a Container Apps service via Service Bus"""
        try:
            from azure.servicebus import ServiceBusClient, ServiceBusMessage
            import json
            
            logger.info(f"[SERVICE MSG] Sending to queue: {queue_name}")
            logger.debug(f"[SERVICE MSG] Data: {message_data}")
            
            client = get_servicebus_client()
            with client:
                sender = client.get_queue_sender(queue_name=queue_name)
                with sender:
                    message = ServiceBusMessage(json.dumps(message_data))
                    sender.send_messages(message)
            
            logger.info(f"[SERVICE MSG] ✅ Message sent to {queue_name}")
            return True
            
        except Exception as e:
            logger.error(f"[SERVICE MSG] ❌ Failed to send to {queue_name}: {e}")
            return False

    def start_web_server(self):
        """Start web server for health checks"""
        def run_server():
            port = int(os.getenv('HTTP_PORT', '8080'))
            try:
                from flask import Flask, jsonify
                
                app = Flask(__name__)
                
                @app.route('/health')
                def health_check():
                    return jsonify({
                        'status': 'healthy',
                        'service': 'import-sal-orchestrator',
                        'active_imports': len(self.orchestrator.active_imports),
                        'active_rollbacks': len(self.orchestrator.active_rollbacks)
                    })
                
                @app.route('/')
                def root():
                    return jsonify({
                        'service': 'import-sal-orchestrator',
                        'version': 'v5',
                        'status': 'running'
                    })
                
                logger.info(f"[WEB SERVER] Starting on port {port}")
                app.run(host='0.0.0.0', port=port, debug=False)
                
            except ImportError:
                logger.warning("[WEB SERVER] Flask not available, skipping web server")
            except Exception as e:
                logger.error(f"[WEB SERVER] Error: {e}")

        web_thread = threading.Thread(target=run_server, name="WebServer")
        web_thread.daemon = True
        web_thread.start()
        logger.info("[WEB SERVER] Started in background thread")