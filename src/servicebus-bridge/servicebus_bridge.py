"""
Azure Service Bus HTTP Bridge v12 - Consuma code Service Bus e chiama endpoint HTTP
Sostituisce il vecchio RabbitMQ bridge con Azure Service Bus
VERSIONE 12: Logging massiccio per debug catena stati
"""
import sys
import os

# Carica variabili da .env (se presente, per sviluppo locale)
from dotenv import load_dotenv
load_dotenv()

# PRINT IMMEDIATI ALL'AVVIO
print("[BRIDGE] ########################################", flush=True)
print("[BRIDGE] ########################################", flush=True)
print("[BRIDGE] STARTING SERVICE BUS BRIDGE v12", flush=True)
print("[BRIDGE] ########################################", flush=True)
print(f"[BRIDGE] ENV CHECK:", flush=True)

_sb_ns = os.getenv('SERVICE_BUS_NAMESPACE', '')
_sb_conn = os.getenv('SERVICE_BUS_CONNECTION_STRING', '')
print(f"[BRIDGE]   SERVICE_BUS_NAMESPACE: {_sb_ns if _sb_ns else '*** NOT SET ***'}", flush=True)
print(f"[BRIDGE]   SERVICE_BUS_CONNECTION_STRING (legacy): {'SET (len=' + str(len(_sb_conn)) + ')' if _sb_conn else 'not set'}", flush=True)
print(f"[BRIDGE]   POLIS_AI_URL: {os.getenv('POLIS_AI_URL', 'NOT SET (using default)')}", flush=True)
print(f"[BRIDGE]   POLIS_API_DATABASE_URL: {os.getenv('POLIS_API_DATABASE_URL', 'NOT SET (using default)')}", flush=True)
print(f"[BRIDGE]   POLIS_SAL_SFTP_URL: {os.getenv('POLIS_SAL_SFTP_URL', 'NOT SET (using default)')}", flush=True)
print(f"[BRIDGE]   POLIS_AUTOGEN_IMPORT_URL: {os.getenv('POLIS_AUTOGEN_IMPORT_URL', 'NOT SET (using default)')}", flush=True)

_api_key = os.getenv('INTERNAL_API_KEY', '')
print(f"[BRIDGE]   INTERNAL_API_KEY: {'SET (len=' + str(len(_api_key)) + ', starts=' + _api_key[:10] + '...)' if _api_key else '*** NOT SET ***'}", flush=True)
print("[BRIDGE] ########################################", flush=True)
sys.stdout.flush()

import json
import logging
import requests
from azure.servicebus import ServiceBusReceiveMode
from azure.servicebus.exceptions import ServiceBusError
from azure_clients import get_servicebus_client
import threading
import time
import traceback

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ServiceBusHTTPBridge:
    def __init__(self, django_base_url=None):
        # Autenticazione Service Bus: SERVICE_BUS_NAMESPACE + managed identity (preferita),
        # SERVICE_BUS_CONNECTION_STRING come fallback legacy (vedi azure_clients.py)

        # URL dei vari servizi (tutti configurabili da .env)
        self.polis_ai_url = django_base_url or os.getenv('POLIS_AI_URL', 'https://polis-ai-gwgfdcctcqaybwds.westeurope-01.azurewebsites.net')
        self.polis_api_database_url = os.getenv('POLIS_API_DATABASE_URL', 'https://polis-api-database-f8fvgqfgd3gpdthd.westeurope-01.azurewebsites.net')
        self.polis_sal_sftp_url = os.getenv('POLIS_SAL_SFTP_URL', 'https://polis-sal-sftp-api.azurewebsites.net')
        self.polis_autogen_import_url = os.getenv('POLIS_AUTOGEN_IMPORT_URL', 'https://polis-autogen-import.azurewebsites.net')
        
        # API key per endpoint interni (senza JWT)
        self.internal_api_key = os.getenv('INTERNAL_API_KEY', '')
        self.client = None
        
        # DEDUPLICAZIONE: Tracciamento messaggi processati
        self.processed_messages = set()
        
        # Mapping code -> endpoint
        self.queue_endpoints = {
            'instradatore': f'{self.polis_ai_url}/api/agents/instradatore',
            'incapsulatore': f'{self.polis_ai_url}/api/agents/incapsulatore', 
            'personale': f'{self.polis_ai_url}/api/agents/personale',
            'sal': f'{self.polis_ai_url}/api/agents/sal',
            'validation': f'{self.polis_ai_url}/api/batch-validate',
            'campionamento': 'DYNAMIC',
            'import-response': 'DYNAMIC_IMPORT'
        }
        
        print(f"[BRIDGE] ========================================", flush=True)
        print(f"[BRIDGE] CONFIGURAZIONE SERVIZI:", flush=True)
        print(f"[BRIDGE]   polis-ai:              {self.polis_ai_url}", flush=True)
        print(f"[BRIDGE]   polis-api-database:     {self.polis_api_database_url}", flush=True)
        print(f"[BRIDGE]   polis-sal-sftp (import): {self.polis_sal_sftp_url}", flush=True)
        print(f"[BRIDGE]   polis-autogen-import:   {self.polis_autogen_import_url}", flush=True)
        print(f"[BRIDGE]   internal_api_key:       {'SET' if self.internal_api_key else '*** VUOTA ***'}", flush=True)
        print(f"[BRIDGE] ========================================", flush=True)
        print(f"[BRIDGE] CODE CONFIGURATE:", flush=True)
        for q, e in self.queue_endpoints.items():
            print(f"[BRIDGE]   {q} -> {e}", flush=True)
        print(f"[BRIDGE] ========================================", flush=True)
        print(f"[BRIDGE] Deduplication: ENABLED", flush=True)
        sys.stdout.flush()
        
        self.receivers = {}
        self.active_threads = []
    
    def connect(self):
        """Connetti a Azure Service Bus"""
        try:
            print(f"[BRIDGE][CONNECT] Tentativo connessione a Service Bus...", flush=True)
            self.client = get_servicebus_client(
                namespace_var='SERVICE_BUS_NAMESPACE',
                conn_str_var='SERVICE_BUS_CONNECTION_STRING',
            )
            print(f"[BRIDGE][CONNECT] Connesso a Azure Service Bus con SUCCESSO", flush=True)
            return True
        except Exception as e:
            print(f"[BRIDGE][CONNECT] *** ERRORE connessione Service Bus: {e} ***", flush=True)
            print(f"[BRIDGE][CONNECT] Traceback: {traceback.format_exc()}", flush=True)
            return False
    
    def process_message(self, queue_name, endpoint_url, message):
        """Processa un singolo messaggio con deduplicazione"""
        try:
            print(f"[BRIDGE][MSG] ========================================", flush=True)
            print(f"[BRIDGE][MSG] Messaggio ricevuto da coda: '{queue_name}'", flush=True)
            
            # Deserializza il messaggio
            message_body = str(message)
            print(f"[BRIDGE][MSG] Raw body (primi 500 char): {message_body[:500]}", flush=True)
            
            try:
                message_data = json.loads(message_body)
            except json.JSONDecodeError as e:
                print(f"[BRIDGE][MSG] *** ERRORE JSON DECODE: {e} ***", flush=True)
                print(f"[BRIDGE][MSG] Body non parsabile: {message_body[:300]}", flush=True)
                return False
            
            process_id = message_data.get('process_id', 'N/A')
            print(f"[BRIDGE][MSG] Process ID: {process_id}", flush=True)
            print(f"[BRIDGE][MSG] Campi messaggio: {list(message_data.keys())}", flush=True)
            
            # CONTROLLO DEDUPLICAZIONE (SKIP per validation e import-response)
            if queue_name not in ('validation', 'import-response') and process_id != 'N/A' and process_id in self.processed_messages:
                print(f"[BRIDGE][MSG] *** DUPLICATO: process_id={process_id} gia' processato - SKIP ***", flush=True)
                return True
            
            # PROCESSO IL MESSAGGIO
            print(f"[BRIDGE][MSG] Avvio processing per coda '{queue_name}'...", flush=True)
            success = self._actual_process_message(queue_name, endpoint_url, message_data)
            
            print(f"[BRIDGE][MSG] Risultato processing: {'SUCCESSO' if success else '*** FALLITO ***'}", flush=True)
            
            # Se successo, marca come processato
            if success and process_id != 'N/A':
                self.processed_messages.add(process_id)
                
                # Limita dimensione set (mantieni solo ultimi 1000)
                if len(self.processed_messages) > 1000:
                    old_messages = list(self.processed_messages)[:200]
                    for old_id in old_messages:
                        self.processed_messages.discard(old_id)
            
            print(f"[BRIDGE][MSG] ========================================", flush=True)
            sys.stdout.flush()
            return success
            
        except Exception as e:
            print(f"[BRIDGE][MSG] *** ECCEZIONE in process_message: {e} ***", flush=True)
            print(f"[BRIDGE][MSG] Traceback: {traceback.format_exc()}", flush=True)
            return False

    def _actual_process_message(self, queue_name, endpoint_url, message_data):
        """Logica originale di processing del messaggio"""
        try:
            # ========== GESTIONE SPECIALE PER CODA VALIDATION ==========
            if queue_name == 'validation':
                print("[BRIDGE][VALIDATION] Async validation processing", flush=True)
                print(f"[BRIDGE][VALIDATION] Calling: {endpoint_url}", flush=True)
                
                response = requests.post(
                    endpoint_url,
                    json=message_data,
                    headers={'Content-Type': 'application/json'},
                    timeout=1800
                )
                
                print(f"[BRIDGE][VALIDATION] Response status: {response.status_code}", flush=True)
                
                if response.status_code in [200, 201, 202]:
                    print(f"[BRIDGE][VALIDATION] OK", flush=True)
                    try:
                        response_data = response.json()
                        print(f"[BRIDGE][VALIDATION]   SAL: {response_data.get('sal', 'N/A')}, Status: {response_data.get('status', 'N/A')}", flush=True)
                    except:
                        pass
                    return True
                else:
                    print(f"[BRIDGE][VALIDATION] *** FALLITO: {response.status_code} ***", flush=True)
                    print(f"[BRIDGE][VALIDATION] Response: {response.text[:500]}", flush=True)
                    return False
            
            # ========== GESTIONE SPECIALE PER CODA CAMPIONAMENTO ==========
            elif queue_name == 'campionamento':
                print("[BRIDGE][CAMPIONAMENTO] Processing campionamento", flush=True)
                
                numero_sal = message_data.get('numero_sal')
                if not numero_sal:
                    print("[BRIDGE][CAMPIONAMENTO] *** ERRORE: numero_sal mancante ***", flush=True)
                    return False
                
                file_paths = message_data.get('file_paths', [])
                if not file_paths:
                    print("[BRIDGE][CAMPIONAMENTO] *** ERRORE: nessun file_path ***", flush=True)
                    return False
                
                specific_endpoint_url = f"{self.polis_api_database_url}/api/v1/sal/{numero_sal}/campionamento"
                request_body = {'file_paths': file_paths}
                
                print(f"[BRIDGE][CAMPIONAMENTO] URL: {specific_endpoint_url}", flush=True)
                print(f"[BRIDGE][CAMPIONAMENTO] Files: {len(file_paths)}", flush=True)
                
                response = requests.post(
                    specific_endpoint_url,
                    json=request_body,
                    headers={'Content-Type': 'application/json'},
                    timeout=120
                )
                
                print(f"[BRIDGE][CAMPIONAMENTO] Response status: {response.status_code}", flush=True)
                
                if response.status_code in [200, 201, 202]:
                    print(f"[BRIDGE][CAMPIONAMENTO] OK SAL {numero_sal}", flush=True)
                    return True
                else:
                    print(f"[BRIDGE][CAMPIONAMENTO] *** FALLITO SAL {numero_sal}: {response.status_code} ***", flush=True)
                    print(f"[BRIDGE][CAMPIONAMENTO] Response: {response.text[:500]}", flush=True)
                    return False
            
            # ========== GESTIONE SPECIALE PER CODA IMPORT-RESPONSE ==========
            elif queue_name == 'import-response':
                print(f"[BRIDGE][IMPORT-RESPONSE] ########################################", flush=True)
                print(f"[BRIDGE][IMPORT-RESPONSE] RICEVUTO messaggio import-response!", flush=True)
                print(f"[BRIDGE][IMPORT-RESPONSE] ########################################", flush=True)
                
                import_type = message_data.get('import_type', 'excel')
                upload_id = message_data.get('upload_id')
                status = message_data.get('status', 'unknown')
                summary = message_data.get('summary', {})
                file_results = message_data.get('file_results', [])
                failed_files = message_data.get('failed_files', [])
                task = message_data.get('task', 'N/A')
                sal = message_data.get('sal', 'N/A')
                sal_number = message_data.get('sal_number', 'N/A')

                print(f"[BRIDGE][IMPORT-RESPONSE] import_type:  '{import_type}'", flush=True)
                print(f"[BRIDGE][IMPORT-RESPONSE] upload_id:    {upload_id}", flush=True)
                print(f"[BRIDGE][IMPORT-RESPONSE] status:       '{status}'", flush=True)
                print(f"[BRIDGE][IMPORT-RESPONSE] task:         '{task}'", flush=True)
                print(f"[BRIDGE][IMPORT-RESPONSE] sal:          '{sal}'", flush=True)
                print(f"[BRIDGE][IMPORT-RESPONSE] sal_number:   {sal_number}", flush=True)
                print(f"[BRIDGE][IMPORT-RESPONSE] summary:      {json.dumps(summary)}", flush=True)

                # Log file_results con errori per-file
                if file_results:
                    print(f"[BRIDGE][IMPORT-RESPONSE] file_results: {len(file_results)} file(s)", flush=True)
                    for fr in file_results:
                        fr_status = fr.get('status', 'N/A')
                        fr_blob = fr.get('blob_path', 'N/A')
                        fr_errors = fr.get('rows_errors', 0)
                        print(f"[BRIDGE][IMPORT-RESPONSE]   - {fr_blob}: status={fr_status}, rows_errors={fr_errors}", flush=True)

                if failed_files:
                    print(f"[BRIDGE][IMPORT-RESPONSE] failed_files: {json.dumps(failed_files)[:300]}", flush=True)
                print(f"[BRIDGE][IMPORT-RESPONSE] TUTTI I CAMPI: {json.dumps(message_data)[:800]}", flush=True)
                
                if not upload_id:
                    print(f"[BRIDGE][IMPORT-RESPONSE] *** ERRORE: upload_id MANCANTE nel messaggio! ***", flush=True)
                    print(f"[BRIDGE][IMPORT-RESPONSE] Messaggio completo: {json.dumps(message_data)}", flush=True)
                    return False
                
                # Determina endpoint da chiamare
                if import_type == 'pdf':
                    callback_url = f"{self.polis_sal_sftp_url}/api/internal/pdf-import-completed"
                    print(f"[BRIDGE][IMPORT-RESPONSE] Tipo PDF -> chiamo pdf-import-completed", flush=True)
                else:
                    callback_url = f"{self.polis_sal_sftp_url}/api/internal/excel-import-completed"
                    print(f"[BRIDGE][IMPORT-RESPONSE] Tipo EXCEL -> chiamo excel-import-completed", flush=True)
                
                print(f"[BRIDGE][IMPORT-RESPONSE] Callback URL: {callback_url}", flush=True)
                
                # Prepara body per l'API - inoltra tutto il payload originale
                request_body = {
                    'upload_id': upload_id,
                    'import_type': import_type,
                    'status': status,
                    'summary': summary,
                    'file_results': file_results,
                    'failed_files': failed_files
                }
                
                print(f"[BRIDGE][IMPORT-RESPONSE] Request body keys: {list(request_body.keys())}", flush=True)
                print(f"[BRIDGE][IMPORT-RESPONSE] Request body (troncato): {json.dumps(request_body)[:1000]}", flush=True)
                
                headers = {'Content-Type': 'application/json'}
                if self.internal_api_key:
                    headers['X-Internal-Api-Key'] = self.internal_api_key
                    print(f"[BRIDGE][IMPORT-RESPONSE] Header X-Internal-Api-Key: SET (len={len(self.internal_api_key)})", flush=True)
                else:
                    print(f"[BRIDGE][IMPORT-RESPONSE] *** ATTENZIONE: INTERNAL_API_KEY vuota! La chiamata fallira' con 401! ***", flush=True)
                
                print(f"[BRIDGE][IMPORT-RESPONSE] Invio richiesta HTTP POST...", flush=True)
                sys.stdout.flush()
                
                try:
                    response = requests.post(
                        callback_url,
                        json=request_body,
                        headers=headers,
                        timeout=None
                    )
                except requests.exceptions.ConnectionError as e:
                    print(f"[BRIDGE][IMPORT-RESPONSE] *** CONNECTION ERROR: {e} ***", flush=True)
                    print(f"[BRIDGE][IMPORT-RESPONSE] L'API {callback_url} non e' raggiungibile!", flush=True)
                    return False
                except requests.exceptions.Timeout:
                    print(f"[BRIDGE][IMPORT-RESPONSE] *** TIMEOUT dopo 120 secondi ***", flush=True)
                    return False
                
                print(f"[BRIDGE][IMPORT-RESPONSE] Response status: {response.status_code}", flush=True)
                
                try:
                    response_text = response.text[:1000]
                    print(f"[BRIDGE][IMPORT-RESPONSE] Response body: {response_text}", flush=True)
                except:
                    pass
                
                if response.status_code in [200, 201, 202]:
                    print(f"[BRIDGE][IMPORT-RESPONSE] *** SUCCESSO! ***", flush=True)
                    try:
                        response_data = response.json()
                        new_stato = response_data.get('stato', 'N/A')
                        report_info = response_data.get('report', {})
                        print(f"[BRIDGE][IMPORT-RESPONSE] Nuovo stato DB: '{new_stato}'", flush=True)
                        if report_info:
                            print(f"[BRIDGE][IMPORT-RESPONSE] Report: {json.dumps(report_info)[:300]}", flush=True)
                    except Exception as e:
                        print(f"[BRIDGE][IMPORT-RESPONSE] Errore parsing response JSON: {e}", flush=True)
                    
                    print(f"[BRIDGE][IMPORT-RESPONSE] ########################################", flush=True)
                    return True
                elif response.status_code == 401:
                    print(f"[BRIDGE][IMPORT-RESPONSE] *** 401 UNAUTHORIZED! ***", flush=True)
                    print(f"[BRIDGE][IMPORT-RESPONSE] INTERNAL_API_KEY probabilmente errata o mancante!", flush=True)
                    print(f"[BRIDGE][IMPORT-RESPONSE] API key usata: '{self.internal_api_key[:20]}...'", flush=True)
                    print(f"[BRIDGE][IMPORT-RESPONSE] ########################################", flush=True)
                    return False
                elif response.status_code == 404:
                    print(f"[BRIDGE][IMPORT-RESPONSE] *** 404 NOT FOUND! ***", flush=True)
                    print(f"[BRIDGE][IMPORT-RESPONSE] upload_id={upload_id} non trovato nel DB oppure endpoint non esiste!", flush=True)
                    print(f"[BRIDGE][IMPORT-RESPONSE] URL chiamato: {callback_url}", flush=True)
                    print(f"[BRIDGE][IMPORT-RESPONSE] ########################################", flush=True)
                    return False
                else:
                    print(f"[BRIDGE][IMPORT-RESPONSE] *** FALLITO: HTTP {response.status_code} ***", flush=True)
                    print(f"[BRIDGE][IMPORT-RESPONSE] Response: {response.text[:500]}", flush=True)
                    print(f"[BRIDGE][IMPORT-RESPONSE] ########################################", flush=True)
                    return False
            
            # ========== GESTIONE STANDARD PER ALTRE CODE ==========
            else:
                print(f"[BRIDGE][STANDARD] Processing coda '{queue_name}'", flush=True)
                print(f"[BRIDGE][STANDARD] Calling: {endpoint_url}", flush=True)
                
                response = requests.post(
                    endpoint_url,
                    json=message_data,
                    headers={'Content-Type': 'application/json'},
                    timeout=30
                )
                
                print(f"[BRIDGE][STANDARD] Response status: {response.status_code}", flush=True)
                
                if response.status_code in [200, 201, 202]:
                    print(f"[BRIDGE][STANDARD] OK {queue_name}", flush=True)
                    return True
                else:
                    print(f"[BRIDGE][STANDARD] *** FALLITO {queue_name}: {response.status_code} ***", flush=True)
                    print(f"[BRIDGE][STANDARD] Response: {response.text[:500]}", flush=True)
                    return False
                    
        except json.JSONDecodeError as e:
            print(f"[BRIDGE][ERROR] JSON decode error: {e}", flush=True)
            return False
        except requests.exceptions.Timeout:
            print(f"[BRIDGE][ERROR] *** TIMEOUT calling {queue_name} ***", flush=True)
            return False
        except requests.exceptions.ConnectionError as e:
            print(f"[BRIDGE][ERROR] *** CONNECTION ERROR {queue_name}: {e} ***", flush=True)
            return False
        except Exception as e:
            print(f"[BRIDGE][ERROR] *** ECCEZIONE GENERALE {queue_name}: {e} ***", flush=True)
            print(f"[BRIDGE][ERROR] Traceback: {traceback.format_exc()}", flush=True)
            return False
    
    def create_queue_consumer(self, queue_name, endpoint_url):
        """Crea un consumer per una specifica coda"""
        def consumer_thread():
            try:
                print(f"[BRIDGE][CONSUMER] ---- Avvio consumer per '{queue_name}' ----", flush=True)
                
                receiver = self.client.get_queue_receiver(
                    queue_name=queue_name,
                    receive_mode=ServiceBusReceiveMode.RECEIVE_AND_DELETE
                )
                self.receivers[queue_name] = receiver
                print(f"[BRIDGE][CONSUMER] Receiver creato per '{queue_name}' (RECEIVE_AND_DELETE)", flush=True)
                
                with receiver:
                    print(f"[BRIDGE][CONSUMER] In ascolto su '{queue_name}'...", flush=True)
                    sys.stdout.flush()
                    
                    while True:
                        try:
                            received_msgs = receiver.receive_messages(max_message_count=1, max_wait_time=5)
                            
                            if received_msgs:
                                for message in received_msgs:
                                    print(f"[BRIDGE][CONSUMER] *** MESSAGGIO RICEVUTO da '{queue_name}'! ***", flush=True)
                                    sys.stdout.flush()
                                    
                                    success = self.process_message(queue_name, endpoint_url, message)

                                    if success:
                                        print(f"[BRIDGE][CONSUMER] Messaggio PROCESSATO per '{queue_name}'", flush=True)
                                    else:
                                        print(f"[BRIDGE][CONSUMER] Messaggio FALLITO per '{queue_name}' (messaggio gia' rimosso dalla coda)", flush=True)
                                    
                                    sys.stdout.flush()
                                    
                        except ServiceBusError as e:
                            print(f"[BRIDGE][CONSUMER] *** Service Bus error per '{queue_name}': {e} ***", flush=True)
                            time.sleep(5)
                        except Exception as e:
                            print(f"[BRIDGE][CONSUMER] *** Errore generico consumer '{queue_name}': {e} ***", flush=True)
                            print(f"[BRIDGE][CONSUMER] Traceback: {traceback.format_exc()}", flush=True)
                            time.sleep(5)
                            
            except Exception as e:
                print(f"[BRIDGE][CONSUMER] *** ERRORE CRITICO consumer '{queue_name}': {e} ***", flush=True)
                print(f"[BRIDGE][CONSUMER] Traceback: {traceback.format_exc()}", flush=True)
        
        return consumer_thread
    
    def start_consuming(self):
        """Avvia tutti i consumer in thread separati"""
        try:
            print("[BRIDGE][START] ========================================", flush=True)
            print("[BRIDGE][START] Avvio consumers...", flush=True)
            print(f"[BRIDGE][START] Code configurate: {list(self.queue_endpoints.keys())}", flush=True)
            print(f"[BRIDGE][START] Totale code: {len(self.queue_endpoints)}", flush=True)
            print("[BRIDGE][START] ========================================", flush=True)
            
            for queue_name, endpoint_url in self.queue_endpoints.items():
                print(f"[BRIDGE][START] Creo consumer per '{queue_name}'...", flush=True)
                consumer_thread = self.create_queue_consumer(queue_name, endpoint_url)
                thread = threading.Thread(target=consumer_thread, daemon=True)
                thread.start()
                self.active_threads.append(thread)
                print(f"[BRIDGE][START] Thread avviato per '{queue_name}' OK", flush=True)
            
            print("[BRIDGE][START] ========================================", flush=True)
            print(f"[BRIDGE][START] TUTTI I {len(self.active_threads)} CONSUMERS AVVIATI", flush=True)
            print("[BRIDGE][START] ========================================", flush=True)
            sys.stdout.flush()
            
            # Mantieni il main thread attivo
            try:
                while True:
                    time.sleep(30)
                    alive_count = sum(1 for t in self.active_threads if t.is_alive())
                    print(f"[BRIDGE][HEARTBEAT] Bridge alive - threads: {alive_count}/{len(self.active_threads)}, processed: {len(self.processed_messages)}", flush=True)
                    
                    if alive_count < len(self.active_threads):
                        dead_threads = len(self.active_threads) - alive_count
                        print(f"[BRIDGE][HEARTBEAT] *** ATTENZIONE: {dead_threads} thread MORTI! ***", flush=True)
                    
                    sys.stdout.flush()
                    
            except KeyboardInterrupt:
                print("[BRIDGE] Stopping...", flush=True)
                self.stop_consuming()
                
        except Exception as e:
            print(f"[BRIDGE][START] *** ERRORE in start_consuming: {e} ***", flush=True)
            print(f"[BRIDGE][START] Traceback: {traceback.format_exc()}", flush=True)
    
    def stop_consuming(self):
        """Ferma tutti i consumer"""
        try:
            print("[BRIDGE][STOP] Stopping all consumers...", flush=True)
            
            for queue_name, receiver in self.receivers.items():
                try:
                    receiver.close()
                    print(f"[BRIDGE][STOP] Receiver '{queue_name}' closed", flush=True)
                except:
                    pass
            
            if self.client:
                self.client.close()
                print("[BRIDGE][STOP] Service Bus client closed", flush=True)
                
        except Exception as e:
            print(f"[BRIDGE][STOP] Errore: {e}", flush=True)

def main():
    """Main function"""
    print("[BRIDGE MAIN] ########################################", flush=True)
    print("[BRIDGE MAIN] Starting main function...", flush=True)
    print("[BRIDGE MAIN] ########################################", flush=True)
    sys.stdout.flush()
    
    try:
        bridge = ServiceBusHTTPBridge()
        print("[BRIDGE MAIN] Bridge instance created", flush=True)
        
        if not bridge.connect():
            print("[BRIDGE MAIN] *** FAILED TO CONNECT - EXITING ***", flush=True)
            return
        
        print("[BRIDGE MAIN] Connected successfully, starting consumers...", flush=True)
        sys.stdout.flush()
        
        bridge.start_consuming()
        
    except Exception as e:
        print(f"[BRIDGE MAIN] *** FATAL ERROR: {e} ***", flush=True)
        print(f"[BRIDGE MAIN] Traceback: {traceback.format_exc()}", flush=True)

if __name__ == "__main__":
    print("[BRIDGE] Script started, calling main()...", flush=True)
    sys.stdout.flush()
    main()
