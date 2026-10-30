#!/usr/bin/env python3
"""
Import SAL Service Manager - Gestisce lancio e risposta dei servizi
Coordina l'esecuzione dei vari servizi (SFTP, backup, import, etc.)
"""
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

class ImportSalServiceManager:
    def __init__(self, orchestrator):
        """Initialize with reference to main orchestrator"""
        self.orchestrator = orchestrator
    
    def handle_service_response(self, task, sal, status, response):
        """Central dispatcher for all service responses"""
        if task == 'sftp-verify-copy':
            self.handle_sftp_response(sal, status)
        elif task == 'db-backup':
            self.handle_db_backup_response(sal, status, response)
        elif task == 'container-backup':
            self.handle_container_backup_response(sal, status, response)
        elif task == 'documentation-setup':
            self.handle_documentation_response(sal, status)
        elif task == 'personale-import':
            self.handle_personale_response(sal, status)
        elif task == 'fatture-import':
            self.handle_fatture_response(sal, status)
        elif task == 'aq-import':
            self.handle_aq_response(sal, status)
        elif task == 'bdc-import':
            self.handle_bdc_response(sal, status)
        elif task == 'fatture-pdf-import':
            self.handle_fatture_pdf_response(sal, status)
        elif task == 'iva-indetraibile-import':
            self.handle_iva_indetraibile_response(sal, status)
        elif task == 'lettere-incarico-import':
            self.handle_lettere_incarico_response(sal, status)
        elif task == 'nota-metodologica-import':
            self.handle_nota_metodologica_response(sal, status)
        elif task == 'nota-trasmissione-import':
            self.handle_nota_trasmissione_response(sal, status)
        elif task == 'passaggi-livello-import':
            self.handle_passaggi_livello_response(sal, status)
        elif task == 'timesheet-import':
            self.handle_timesheet_response(sal, status)
        elif task == 'vap-import':
            self.handle_vap_response(sal, status)
        elif task == 'db-rollback':
            self.handle_db_rollback_response(sal, status, response)
        elif task == 'db-rollback-sal':
            self.handle_db_rollback_sal_response(sal, status, response)
        elif task == 'storage-cleanup':
            self.handle_storage_cleanup_response(sal, status)
        else:
            logger.warning(f"[RESPONSE] Unknown task: {task}")

    # Import flow handlers
    def handle_sftp_response(self, sal, status):
        """Handle SFTP service response"""
        logger.info(f"[IMPORT] Processing SFTP response for {sal}, status: {status}")
        
        if status == 'success':
            self.orchestrator.utils.send_notification(sal, "Import", "Copia file da SFTP completata con successo")
            logger.info(f"[IMPORT] ✅ SFTP completed for {sal}")
            
            # Update import status and launch DB backup
            if sal in self.orchestrator.active_imports:
                self.orchestrator.active_imports[sal]['current_step'] = 'db-backup'

            logger.info(f"[IMPORT] Launching DB BACKUP...")
            self.launch_db_backup(sal)

        else:
            self.orchestrator.utils.send_notification(sal, "Import", "Copia file da SFTP fallita")
            logger.error(f"[IMPORT] ❌ SFTP failed for {sal}")
            self.launch_storage_cleanup(sal, "SFTP process failed")

    def handle_db_backup_response(self, sal, status, response):
        """Handle DB backup service response"""
        container_name = f"db-backup-{sal.lower().replace('_', '-')}"

        
        if status == 'success':
            backup_path = response.get('backup_path')

            if backup_path:
                self.orchestrator.db_backup_paths[sal] = backup_path
                if sal in self.orchestrator.active_imports:
                    self.orchestrator.active_imports[sal]['db_backup_path'] = backup_path
                    self.orchestrator.active_imports[sal]['current_step'] = 'container-backup'
                logger.info(f"[IMPORT] ✅ DB backup stored: {backup_path}")
            self.orchestrator.utils.send_notification(sal, "Import", "Copia DB Backup completata con successo")
            logger.info(f"[IMPORT] DB Backup completed for {sal}")
            logger.info(f"[IMPORT] Launching CONTAINER BACKUP...")
            
            # NEW STEP: Container backup before data import
            self.launch_container_backup(sal)
        else:
            logger.error(f"[IMPORT] DB Backup failed for {sal}")
            self.launch_storage_cleanup(sal, "DB Backup process failed")
            self.orchestrator.utils.send_notification(sal, "Import", "Copia DB Backup fallita")

    def handle_container_backup_response(self, sal, status, response):
        """Handle container backup service response"""
        container_name = f"container-backup-{sal.lower().replace('_', '-')}"

        
        if status == 'success':
            stats = response.get('stats', {})
            backup_paths = stats.get('backup_paths', [])
            
            if backup_paths:
                self.orchestrator.container_backup_paths[sal] = backup_paths
                if sal in self.orchestrator.active_imports:
                    self.orchestrator.active_imports[sal]['container_backup_paths'] = backup_paths
                logger.info(f"[IMPORT/ROLLBACK] ✅ Container backup paths: {backup_paths}")
            
            # Check se siamo in un import o rollback
            if sal in self.orchestrator.active_imports and self.orchestrator.active_imports[sal].get('current_step') == 'container-backup':
                # Import flow - continua con documentation
                logger.info(f"[IMPORT] Container backup completed for {sal}")
                self.orchestrator.utils.send_notification(sal, "Import", "Documentale backup completato con successo")
                logger.info(f"[IMPORT] Launching DOCUMENTATION SETUP...")
                self.orchestrator.active_imports[sal]['current_step'] = 'documentation'
                self.launch_documentation_service(sal)
            elif sal in self.orchestrator.active_rollbacks:
                # Rollback flow - procedi con la cancellazione
                logger.info(f"[ROLLBACK] Container backup completed for {sal}")
                self.orchestrator.utils.send_notification(sal, "Rollback", "Documentale backup ripristinato con successo")
                logger.info(f"[ROLLBACK] Launching storage cleanup...")
                self.launch_storage_cleanup(sal, "SAL rollback - cleaning storage")
        else:
            logger.warning(f"[BACKUP] Container backup failed for {sal}")
            self.orchestrator.utils.send_notification(sal, "Import", "Documentale backup falito")
            if sal in self.orchestrator.active_imports:
                # Continue anyway during import
                logger.info(f"[IMPORT] Continuing without container backup...")
                self.launch_documentation_service(sal)
            elif sal in self.orchestrator.active_rollbacks:
                # Continue with deletion even if backup failed
                logger.warning(f"[ROLLBACK] Continuing with deletion despite backup failure...")
                self.launch_storage_cleanup(sal, "SAL rollback - cleaning storage")

    def handle_documentation_response(self, sal, status):
        """Handle documentation service response"""
        container_name = f"documentation-{sal.lower().replace('_', '-')}"

        
        if status == 'success':
            logger.info(f"[IMPORT] Documentation completed for {sal}")
            self.orchestrator.utils.send_notification(sal, "Import", "Documentazione generale SAL SAL aggiornata con successo")
            logger.info(f"[IMPORT] Launching PERSONALE IMPORT...")
            if sal in self.orchestrator.active_imports:
                self.orchestrator.active_imports[sal]['current_step'] = 'personale'
            self.launch_azure_container_personale(sal)
        else:
            logger.error(f"[IMPORT] Documentation failed for {sal}")
            self.rollback_failed_import(sal, "Documentation setup failed")
            self.orchestrator.utils.send_notification(sal, "Import", "Documentazione generale SAL SAL fallita")

    def handle_personale_response(self, sal, status):
        """Handle personale import service response"""
        sal_clean = sal.lower().replace('_', '-')

        if status == 'success':
            logger.info(f"[IMPORT] Personale completed for {sal}")
            logger.info(f"[IMPORT] Launching FATTURE IMPORT...")
            self.orchestrator.utils.send_notification(sal, "Import", "Personale importato con successo")
            if sal in self.orchestrator.active_imports:
                self.orchestrator.active_imports[sal]['current_step'] = 'fatture'
            self.launch_azure_container_fatture(sal)
        else:
            logger.error(f"[IMPORT] Personale failed for {sal}")
            self.orchestrator.utils.send_notification(sal, "Import", "Personale non importato per mancanza/lettura errata del file")
            self.rollback_failed_import(sal, "Personale import failed")

    def handle_fatture_response(self, sal, status):
        """Handle fatture import service response"""

        if status == 'success':
            logger.info("="*60)
            self.orchestrator.utils.send_notification(sal, "Import", "Spese importate con successo")
            if sal in self.orchestrator.active_imports:
                self.orchestrator.active_imports[sal]['current_step'] = 'aq-import'
            logger.info(f"[IMPORT] Launching AQ PDF import...")
            self.launch_azure_container_aq(sal)
            logger.info("="*60)
        else:
            self.orchestrator.utils.send_notification(sal, "Import", "Spese non importate per mancanza/lettura errata del file")
            logger.error(f"[IMPORT] Fatture failed for {sal}")
            self.rollback_failed_import(sal, "Fatture import failed")

    def handle_aq_response(self, sal, status):
        """Handle AQ PDF import service response"""
        sal_clean = sal.lower().replace('_', '-')

        # Always proceed to next step regardless of status
        if status == 'success':
            logger.info(f"[IMPORT] ✅ AQ import completed for {sal}")
            self.orchestrator.utils.send_notification(sal, "Import", "AQ PDF importati con successo")
        else:
            logger.warning(f"[IMPORT] ⚠️ AQ import had issues for {sal} (status: {status}), proceeding anyway")
            self.orchestrator.utils.send_notification(sal, "Import", "AQ PDF")
        if sal in self.orchestrator.active_imports:
            self.orchestrator.active_imports[sal]['current_step'] = 'bdc-import'
        logger.info(f"[IMPORT] Launching BDC PDF import...")
        self.launch_azure_container_bdc(sal)

    def handle_bdc_response(self, sal, status):
        """Handle BDC PDF import service response"""
        sal_clean = sal.lower().replace('_', '-')

        # Always proceed to next step regardless of status
        if status == 'success':
            logger.info(f"[IMPORT] ✅ BDC import completed for {sal}")
            self.orchestrator.utils.send_notification(sal, "Import", "BDC PDF importati con successo")
        else:
            logger.warning(f"[IMPORT] ⚠️ BDC import had issues for {sal} (status: {status}), proceeding anyway")
            self.orchestrator.utils.send_notification(sal, "Import", "BDC PDF")
        if sal in self.orchestrator.active_imports:
            self.orchestrator.active_imports[sal]['current_step'] = 'fatture-pdf-import'
        logger.info(f"[IMPORT] Launching Fatture PDF import...")
        self.launch_azure_container_fatture_pdf(sal)

    def handle_fatture_pdf_response(self, sal, status):
        """Handle Fatture PDF import service response"""

        # Always proceed to next step regardless of status
        if status == 'success':
            logger.info(f"[IMPORT] ✅ Fatture PDF import completed for {sal}")
            self.orchestrator.utils.send_notification(sal, "Import", "Fatture PDF importate con successo")
        else:
            logger.warning(f"[IMPORT] ⚠️ Fatture PDF import had issues for {sal} (status: {status}), proceeding anyway")
            self.orchestrator.utils.send_notification(sal, "Import", "Fatture PDF")
        if sal in self.orchestrator.active_imports:
            self.orchestrator.active_imports[sal]['current_step'] = 'iva-indetraibile-import'
        logger.info(f"[IMPORT] Launching IVA Indetraibile PDF import...")
        self.launch_azure_container_iva_indetraibile(sal)

    def handle_iva_indetraibile_response(self, sal, status):
        """Handle IVA Indetraibile PDF import service response"""

        # Always proceed to next step regardless of status
        if status == 'success':
            logger.info(f"[IMPORT] ✅ IVA Indetraibile import completed for {sal}")
            self.orchestrator.utils.send_notification(sal, "Import", "IVA Indetraibile PDF importati con successo")
        else:
            logger.warning(f"[IMPORT] ⚠️ IVA Indetraibile import had issues for {sal} (status: {status}), proceeding anyway")
            self.orchestrator.utils.send_notification(sal, "Import", "IVA Indetraibile PDF")
        if sal in self.orchestrator.active_imports:
            self.orchestrator.active_imports[sal]['current_step'] = 'lettere-incarico-import'
        logger.info(f"[IMPORT] Launching Lettere Incarico PDF import...")
        self.launch_azure_container_lettere_incarico(sal)

    def handle_lettere_incarico_response(self, sal, status):
        """Handle Lettere Incarico PDF import service response"""

        # Always proceed to next step regardless of status
        if status == 'success':
            logger.info(f"[IMPORT] ✅ Lettere Incarico import completed for {sal}")
            self.orchestrator.utils.send_notification(sal, "Import", "Lettere Incarico PDF importate con successo")
        else:
            logger.warning(f"[IMPORT] ⚠️ Lettere Incarico import had issues for {sal} (status: {status}), proceeding anyway")
            self.orchestrator.utils.send_notification(sal, "Import", "Lettere Incarico PDF")
        if sal in self.orchestrator.active_imports:
            self.orchestrator.active_imports[sal]['current_step'] = 'nota-metodologica-import'
        logger.info(f"[IMPORT] Launching Nota Metodologica PDF import...")
        self.launch_azure_container_nota_metodologica(sal)

    def handle_nota_metodologica_response(self, sal, status):
        """Handle Nota Metodologica PDF import service response"""

        # Always proceed to next step regardless of status
        if status == 'success':
            logger.info(f"[IMPORT] ✅ Nota Metodologica import completed for {sal}")
            self.orchestrator.utils.send_notification(sal, "Import", "Nota Metodologica PDF importate con successo")
        else:
            logger.warning(f"[IMPORT] ⚠️ Nota Metodologica import had issues for {sal} (status: {status}), proceeding anyway")
            self.orchestrator.utils.send_notification(sal, "Import", "Nota Metodologica PDF")
        if sal in self.orchestrator.active_imports:
            self.orchestrator.active_imports[sal]['current_step'] = 'nota-trasmissione-import'
        logger.info(f"[IMPORT] Launching Nota Trasmissione PDF import...")
        self.launch_azure_container_nota_trasmissione(sal)

    def handle_nota_trasmissione_response(self, sal, status):
        """Handle Nota Trasmissione PDF import service response"""

        # Always proceed to next step regardless of status
        if status == 'success':
            logger.info(f"[IMPORT] ✅ Nota Trasmissione import completed for {sal}")
            self.orchestrator.utils.send_notification(sal, "Import", "Nota Trasmissione PDF importate con successo")
        else:
            logger.warning(f"[IMPORT] ⚠️ Nota Trasmissione import had issues for {sal} (status: {status}), proceeding anyway")
            self.orchestrator.utils.send_notification(sal, "Import", "Nota Trasmissione PDF")
        if sal in self.orchestrator.active_imports:
            self.orchestrator.active_imports[sal]['current_step'] = 'passaggi-livello-import'
        logger.info(f"[IMPORT] Launching Passaggi di Livello PDF import...")
        self.launch_azure_container_passaggi_livello(sal)

    def handle_passaggi_livello_response(self, sal, status):
        """Handle Passaggi di Livello PDF import service response"""

        # Always proceed to next step regardless of status
        if status == 'success':
            logger.info(f"[IMPORT] ✅ Passaggi di Livello import completed for {sal}")
            self.orchestrator.utils.send_notification(sal, "Import", "Passaggi di Livello PDF importati con successo")
        else:
            logger.warning(f"[IMPORT] ⚠️ Passaggi di Livello import had issues for {sal} (status: {status}), proceeding anyway")
            self.orchestrator.utils.send_notification(sal, "Import", "Passaggi di Livello PDF")
        if sal in self.orchestrator.active_imports:
            self.orchestrator.active_imports[sal]['current_step'] = 'timesheet-import'
        logger.info(f"[IMPORT] Launching Timesheet PDF import...")
        self.launch_azure_container_timesheet(sal)

    def handle_timesheet_response(self, sal, status):
        """Handle Timesheet PDF import service response"""
        # Always proceed to next step regardless of status
        if status == 'success':
            logger.info(f"[IMPORT] ✅ Timesheet import completed for {sal}")
            self.orchestrator.utils.send_notification(sal, "Import", "Timesheet PDF importati con successo")
        else:
            logger.warning(f"[IMPORT] ⚠️ Timesheet import had issues for {sal} (status: {status}), proceeding anyway")
            self.orchestrator.utils.send_notification(sal, "Import", "Timesheet PDF")
        if sal in self.orchestrator.active_imports:
            self.orchestrator.active_imports[sal]['current_step'] = 'vap-import'
        logger.info(f"[IMPORT] Launching VAP PDF import...")
        self.launch_azure_container_vap(sal)

    def handle_vap_response(self, sal, status):
        """Handle VAP PDF import service response - LAST PDF IMPORT"""

        # Always mark completion and proceed to finalization regardless of status
        if status == 'success':
            logger.info(f"[IMPORT] ✅ VAP import completed for {sal}")
            self.orchestrator.utils.send_notification(sal, "Import", "VAP PDF importati con successo")
        else:
            logger.warning(f"[IMPORT] ⚠️ VAP import had issues for {sal} (status: {status}), proceeding to completion anyway")
            self.orchestrator.utils.send_notification(sal, "Import", "VAP PDF ")
        if sal in self.orchestrator.active_imports:
            self.orchestrator.active_imports[sal]['current_step'] = 'completed'
        logger.info(f"[IMPORT] All PDF imports processed! Marking import as done...")
        self.launch_azure_container_pdf(sal)

    # Rollback handlers
    def handle_db_rollback_response(self, sal, status, response):
        """Handle DB rollback service response"""
        container_name = f"db-rollback-{sal.lower().replace('_', '-')}"

        
        if status == 'success':
            self.orchestrator.utils.send_notification(sal, "Rollback", "Ripristino del database completato con successo")
            logger.info(f"[ROLLBACK] ✅ DB Rollback completed for {sal}")
            if sal in self.orchestrator.db_backup_paths:
                logger.info(f"[ROLLBACK] Restored from: {self.orchestrator.db_backup_paths[sal]}")
            self.launch_storage_cleanup(sal, "Import failed - DB rolled back")
        else:
            self.orchestrator.utils.send_notification(sal, "Rollback", "Ripristino del database fallito")
            logger.error(f"[ROLLBACK] ❌ DB Rollback failed for {sal}")
            self.launch_storage_cleanup(sal, "Import failed - Rollback failed")

    def handle_db_rollback_sal_response(self, sal, status, response):
        """Handle DB rollback SAL (deletion) service response"""
        container_name = f"db-rollback-sal-{sal.lower().replace('_', '-')}"

        
        if status == 'success':
            rows_deleted = response.get('rows_deleted', {})
            total_deleted = sum(rows_deleted.values())
            logger.info(f"[ROLLBACK] ✅ DB deletion completed for {sal}")
            logger.info(f"[ROLLBACK] Total rows deleted: {total_deleted}")
            self.orchestrator.utils.send_notification(sal, "Rollback", "eliminazione ultimo sal dal database completata con successo")
            # Backup container con tipo 'delete' prima della cancellazione
            logger.info(f"[ROLLBACK] Launching container backup before deletion...")
            self.launch_container_backup(sal, backup_type='delete')
        else:
            logger.error(f"[ROLLBACK] ❌ DB deletion failed for {sal}")
            self.orchestrator.utils.send_notification(sal, "Rollback", "Eliminazione ultimo sal dal database fallito")
            if sal in self.orchestrator.active_rollbacks:
                self.orchestrator.active_rollbacks[sal]['status'] = 'failed'

    def handle_storage_cleanup_response(self, sal, status):
        """Handle storage cleanup service response"""
        container_name = f"storage-cleanup-{sal.lower().replace('_', '-')}"

        if status == 'success':
            logger.info(f"[CLEANUP] ✅ Storage cleanup completed for {sal}")
            
            # Check if this is part of a rollback
            if sal in self.orchestrator.active_rollbacks:
                self.orchestrator.active_rollbacks[sal]['status'] = 'completed'
                logger.info("="*60)
                logger.info(f"[ROLLBACK] ✅ ROLLBACK COMPLETED for {sal}")
                self.orchestrator.utils.send_notification(sal, "Rollback", "eliminazione ultimo sal dal documentale completata con successo")
                logger.info("="*60)
            
            # Check if this is part of an import cleanup
            if sal in self.orchestrator.active_imports:
                self.orchestrator.active_imports[sal]['status'] = 'cleaned'
        else:
            logger.error(f"[CLEANUP] ❌ Storage cleanup failed for {sal}")
            self.orchestrator.utils.send_notification(sal, "Rollback", "Eliminazione ultimo sal dal documentale fallito")
            if sal in self.orchestrator.active_imports:
                self.orchestrator.active_imports[sal]['status'] = 'cleanup_failed'
            if sal in self.orchestrator.active_rollbacks:
                self.orchestrator.active_rollbacks[sal]['status'] = 'cleanup_failed'

    # Helper methods
    def rollback_failed_import(self, sal, reason):
        """Rollback after failed import (DB changes need rollback)"""
        logger.info(f"[ROLLBACK] Rolling back failed import for {sal}: {reason}")
        
        if sal in self.orchestrator.active_imports:
            self.orchestrator.active_imports[sal]['status'] = 'rolling_back'
            
            db_backup = self.orchestrator.db_backup_paths.get(sal)
            if db_backup:
                logger.info(f"[ROLLBACK] Restoring DB from: {db_backup}")
                self.launch_db_rollback(sal, db_backup)
            else:
                logger.error(f"[ROLLBACK] No DB backup found for {sal}")
                self.launch_storage_cleanup(sal, reason)

    # Service launchers
    def trigger_sftp_service(self, data):
        """Trigger SFTP Container Apps service via Service Bus"""
        sal_name = data.get('sal_storage_name')
        
        logger.info(f"[SFTP SERVICE] Triggering SFTP service for: {sal_name}")
        
        try:
            import uuid
            
            # Prepare lightweight message for SFTP service
            message_data = {
                'task': 'copy-sftp',
                'sal_storage_name': sal_name,
                'sal_sftp_name': data.get('sal_sftp_name', ''),
                'request_id': str(uuid.uuid4())
            }
            
            logger.info(f"[SFTP SERVICE] Request ID: {message_data['request_id']}")
            
            # Send message to SFTP service queue
            success = self.orchestrator.utils.send_service_message(
                self.orchestrator.SFTP_SERVICE_QUEUE, 
                message_data
            )
            
            if success:
                logger.info(f"[SFTP SERVICE] ✅ SFTP service triggered for {sal_name}")
            else:
                logger.error(f"[SFTP SERVICE] ❌ Failed to trigger SFTP service for {sal_name}")
                # TODO: Handle failure - maybe retry or fallback
                
        except Exception as e:
            logger.error(f"[SFTP SERVICE] Error triggering SFTP service: {e}")
            # TODO: Handle exception - notify failure
    
    def launch_db_backup(self, sal_name):
        """Trigger DB backup service via Service Bus"""
        logger.info(f"[DB-BACKUP] Triggering DB backup service for: {sal_name}")
        
        try:
            import uuid
            
            # Prepare message for DB backup service
            message_data = {
                'task': 'backup-db',
                'sal_storage_name': sal_name,
                'request_id': str(uuid.uuid4())
            }
            
            logger.info(f"[DB-BACKUP] Request ID: {message_data['request_id']}")
            
            # Send message to DB backup service queue
            DB_BACKUP_QUEUE = os.getenv('DB_BACKUP_SERVICE_QUEUE', 'import-sal-db-backup')
            success = self.orchestrator.utils.send_service_message(
                DB_BACKUP_QUEUE, 
                message_data
            )
            
            if success:
                logger.info(f"[DB-BACKUP] ✅ DB backup service triggered for {sal_name}")
            else:
                logger.error(f"[DB-BACKUP] ❌ Failed to trigger DB backup service for {sal_name}")
                
        except Exception as e:
            logger.error(f"[DB-BACKUP] Error triggering DB backup service: {e}")

    def launch_container_backup(self, sal_name, backup_type='import'):
        """Trigger container backup service via Service Bus"""
        logger.info(f"[CONTAINER-BACKUP] Triggering container backup service for: {sal_name}")
        logger.info(f"[CONTAINER-BACKUP] Backup type: {backup_type}")
        
        try:
            import uuid

            # Prepare message for container backup service
            message_data = {
                'task': 'backup-container',
                'sal_storage_name': sal_name,
                'backup_type': backup_type,
                'request_id': str(uuid.uuid4())
            }

            logger.info(f"[CONTAINER-BACKUP] Request ID: {message_data['request_id']}")

            # Send message to container backup service queue
            CONTAINER_BACKUP_QUEUE = os.getenv('CONTAINER_BACKUP_SERVICE_QUEUE', 'import-sal-container-backup')
            success = self.orchestrator.utils.send_service_message(
                CONTAINER_BACKUP_QUEUE,
                message_data
            )
            
            if success:
                logger.info(f"[CONTAINER-BACKUP] ✅ Container backup service triggered for {sal_name}")
            else:
                logger.error(f"[CONTAINER-BACKUP] ❌ Failed to trigger container backup service for {sal_name}")

        except Exception as e:
            logger.error(f"[CONTAINER-BACKUP] Error triggering container backup service: {e}")

    def launch_documentation_service(self, sal_name):
        """Trigger documentation service via Service Bus"""
        logger.info(f"[DOCUMENTATION] Triggering documentation service for: {sal_name}")

        try:
            import uuid

            # Prepare message for documentation service
            message_data = {
                'task': 'documentation-setup',
                'sal_storage_name': sal_name,
                'request_id': str(uuid.uuid4())
            }

            logger.info(f"[DOCUMENTATION] Request ID: {message_data['request_id']}")

            # Send message to documentation service queue
            DOCUMENTATION_QUEUE = os.getenv('DOCUMENTATION_SERVICE_QUEUE', 'import-sal-documentation')
            success = self.orchestrator.utils.send_service_message(
                DOCUMENTATION_QUEUE,
                message_data
            )
            
            if success:
                logger.info(f"[DOCUMENTATION] ✅ Documentation service triggered for {sal_name}")
            else:
                logger.error(f"[DOCUMENTATION] ❌ Failed to trigger documentation service for {sal_name}")

        except Exception as e:
            logger.error(f"[DOCUMENTATION] Error triggering documentation service: {e}")

    def launch_azure_container_personale(self, sal_name):
        """Trigger personale import service via Service Bus"""
        logger.info(f"[PERSONALE] Triggering personale import service for: {sal_name}")

        try:
            import uuid

            # Prepare message for personale import service
            message_data = {
                'task': 'personale-import',
                'sal_storage_name': sal_name,
                'request_id': str(uuid.uuid4())
            }

            logger.info(f"[PERSONALE] Request ID: {message_data['request_id']}")

            # Send message to personale import service queue
            PERSONALE_QUEUE = os.getenv('PERSONALE_SERVICE_QUEUE', 'import-sal-personale')
            success = self.orchestrator.utils.send_service_message(
                PERSONALE_QUEUE,
                message_data
            )

            if success:
                logger.info(f"[PERSONALE] ✅ Personale import service triggered for {sal_name}")
            else:
                logger.error(f"[PERSONALE] ❌ Failed to trigger personale import service for {sal_name}")

        except Exception as e:
            logger.error(f"[PERSONALE] Error triggering personale import service: {e}")

    def launch_azure_container_fatture(self, sal_name):
        """Trigger fatture import service via Service Bus"""
        logger.info(f"[FATTURE] Triggering fatture import service for: {sal_name}")

        try:
            import uuid

            # Prepare message for fatture import service
            message_data = {
                'task': 'fatture-import',
                'sal_storage_name': sal_name,
                'request_id': str(uuid.uuid4())
            }

            logger.info(f"[FATTURE] Request ID: {message_data['request_id']}")

            # Send message to fatture import service queue
            FATTURE_QUEUE = os.getenv('FATTURE_SERVICE_QUEUE', 'import-sal-fatture')
            success = self.orchestrator.utils.send_service_message(
                FATTURE_QUEUE,
                message_data
            )

            if success:
                logger.info(f"[FATTURE] ✅ Fatture import service triggered for {sal_name}")
            else:
                logger.error(f"[FATTURE] ❌ Failed to trigger fatture import service for {sal_name}")

        except Exception as e:
            logger.error(f"[FATTURE] Error triggering fatture import service: {e}")

    def launch_azure_container_aq(self, sal_name):
        """Trigger AQ PDF import service via Service Bus"""
        logger.info(f"[AQ] Triggering AQ PDF import service for: {sal_name}")

        try:
            import uuid

            # Prepare message for AQ import service
            message_data = {
                'task': 'aq-import',
                'sal_storage_name': sal_name,
                'request_id': str(uuid.uuid4())
            }

            logger.info(f"[AQ] Request ID: {message_data['request_id']}")

            # Send message to AQ import service queue
            AQ_QUEUE = os.getenv('AQ_SERVICE_QUEUE', 'import-sal-aq')
            success = self.orchestrator.utils.send_service_message(
                AQ_QUEUE,
                message_data
            )

            if success:
                logger.info(f"[AQ] ✅ AQ import service triggered for {sal_name}")
            else:
                logger.error(f"[AQ] ❌ Failed to trigger AQ import service for {sal_name}")

        except Exception as e:
            logger.error(f"[AQ] Error triggering AQ import service: {e}")

    def launch_azure_container_bdc(self, sal_name):
        """Trigger BDC PDF import service via Service Bus"""
        logger.info(f"[BDC] Triggering BDC PDF import service for: {sal_name}")

        try:
            import uuid

            # Prepare message for BDC import service
            message_data = {
                'task': 'bdc-import',
                'sal_storage_name': sal_name,
                'request_id': str(uuid.uuid4())
            }

            logger.info(f"[BDC] Request ID: {message_data['request_id']}")

            # Send message to BDC import service queue
            BDC_QUEUE = os.getenv('BDC_SERVICE_QUEUE', 'import-sal-bdc')
            success = self.orchestrator.utils.send_service_message(
                BDC_QUEUE,
                message_data
            )

            if success:
                logger.info(f"[BDC] ✅ BDC import service triggered for {sal_name}")
            else:
                logger.error(f"[BDC] ❌ Failed to trigger BDC import service for {sal_name}")

        except Exception as e:
            logger.error(f"[BDC] Error triggering BDC import service: {e}")

    def launch_azure_container_fatture_pdf(self, sal_name):
        """Trigger Fatture PDF import service via Service Bus"""
        logger.info(f"[FATTURE-PDF] Triggering Fatture PDF import service for: {sal_name}")

        try:
            import uuid

            # Prepare message for Fatture PDF import service
            message_data = {
                'task': 'fatture-pdf-import',
                'sal_storage_name': sal_name,
                'request_id': str(uuid.uuid4())
            }

            logger.info(f"[FATTURE-PDF] Request ID: {message_data['request_id']}")

            # Send message to Fatture PDF import service queue
            FATTURE_PDF_QUEUE = os.getenv('FATTURE_PDF_SERVICE_QUEUE', 'import-sal-fatture-pdf')
            success = self.orchestrator.utils.send_service_message(
                FATTURE_PDF_QUEUE,
                message_data
            )

            if success:
                logger.info(f"[FATTURE-PDF] ✅ Fatture PDF import service triggered for {sal_name}")
            else:
                logger.error(f"[FATTURE-PDF] ❌ Failed to trigger Fatture PDF import service for {sal_name}")

        except Exception as e:
            logger.error(f"[FATTURE-PDF] Error triggering Fatture PDF import service: {e}")

    def launch_azure_container_iva_indetraibile(self, sal_name):
        """Trigger IVA Indetraibile PDF import service via Service Bus"""
        logger.info(f"[IVA-INDETRAIBILE] Triggering IVA Indetraibile PDF import service for: {sal_name}")

        try:
            import uuid

            # Prepare message for IVA Indetraibile import service
            message_data = {
                'task': 'iva-indetraibile-import',
                'sal_storage_name': sal_name,
                'request_id': str(uuid.uuid4())
            }

            logger.info(f"[IVA-INDETRAIBILE] Request ID: {message_data['request_id']}")

            # Send message to IVA Indetraibile import service queue
            IVA_INDETRAIBILE_QUEUE = os.getenv('IVA_INDETRAIBILE_SERVICE_QUEUE', 'import-sal-iva-indetraibile')
            success = self.orchestrator.utils.send_service_message(
                IVA_INDETRAIBILE_QUEUE,
                message_data
            )

            if success:
                logger.info(f"[IVA-INDETRAIBILE] ✅ IVA Indetraibile import service triggered for {sal_name}")
            else:
                logger.error(f"[IVA-INDETRAIBILE] ❌ Failed to trigger IVA Indetraibile import service for {sal_name}")

        except Exception as e:
            logger.error(f"[IVA-INDETRAIBILE] Error triggering IVA Indetraibile import service: {e}")

    def launch_azure_container_lettere_incarico(self, sal_name):
        """Trigger Lettere Incarico PDF import service via Service Bus"""
        logger.info(f"[LETTERE-INCARICO] Triggering Lettere Incarico PDF import service for: {sal_name}")

        try:
            import uuid

            # Prepare message for Lettere Incarico import service
            message_data = {
                'task': 'lettere-incarico-import',
                'sal_storage_name': sal_name,
                'request_id': str(uuid.uuid4())
            }

            logger.info(f"[LETTERE-INCARICO] Request ID: {message_data['request_id']}")

            # Send message to Lettere Incarico import service queue
            LETTERE_INCARICO_QUEUE = os.getenv('LETTERE_INCARICO_SERVICE_QUEUE', 'import-sal-lettere-incarico')
            success = self.orchestrator.utils.send_service_message(
                LETTERE_INCARICO_QUEUE,
                message_data
            )

            if success:
                logger.info(f"[LETTERE-INCARICO] ✅ Lettere Incarico import service triggered for {sal_name}")
            else:
                logger.error(f"[LETTERE-INCARICO] ❌ Failed to trigger Lettere Incarico import service for {sal_name}")

        except Exception as e:
            logger.error(f"[LETTERE-INCARICO] Error triggering Lettere Incarico import service: {e}")

    def launch_azure_container_nota_metodologica(self, sal_name):
        """Trigger Nota Metodologica PDF import service via Service Bus"""
        logger.info(f"[NOTA-METODOLOGICA] Triggering Nota Metodologica PDF import service for: {sal_name}")

        try:
            import uuid

            # Prepare message for Nota Metodologica import service
            message_data = {
                'task': 'nota-metodologica-import',
                'sal_storage_name': sal_name,
                'request_id': str(uuid.uuid4())
            }

            logger.info(f"[NOTA-METODOLOGICA] Request ID: {message_data['request_id']}")

            # Send message to Nota Metodologica import service queue
            NOTA_METODOLOGICA_QUEUE = os.getenv('NOTA_METODOLOGICA_SERVICE_QUEUE', 'import-sal-nota-metodologica')
            success = self.orchestrator.utils.send_service_message(
                NOTA_METODOLOGICA_QUEUE,
                message_data
            )

            if success:
                logger.info(f"[NOTA-METODOLOGICA] ✅ Nota Metodologica import service triggered for {sal_name}")
            else:
                logger.error(f"[NOTA-METODOLOGICA] ❌ Failed to trigger Nota Metodologica import service for {sal_name}")

        except Exception as e:
            logger.error(f"[NOTA-METODOLOGICA] Error triggering Nota Metodologica import service: {e}")

    def launch_azure_container_nota_trasmissione(self, sal_name):
        """Trigger Nota Trasmissione PDF import service via Service Bus"""
        logger.info(f"[NOTA-TRASMISSIONE] Triggering Nota Trasmissione PDF import service for: {sal_name}")

        try:
            import uuid

            # Prepare message for Nota Trasmissione import service
            message_data = {
                'task': 'nota-trasmissione-import',
                'sal_storage_name': sal_name,
                'request_id': str(uuid.uuid4())
            }

            logger.info(f"[NOTA-TRASMISSIONE] Request ID: {message_data['request_id']}")

            # Send message to Nota Trasmissione import service queue
            NOTA_TRASMISSIONE_QUEUE = os.getenv('NOTA_TRASMISSIONE_SERVICE_QUEUE', 'import-sal-nota-trasmissione')
            success = self.orchestrator.utils.send_service_message(
                NOTA_TRASMISSIONE_QUEUE,
                message_data
            )

            if success:
                logger.info(f"[NOTA-TRASMISSIONE] ✅ Nota Trasmissione import service triggered for {sal_name}")
            else:
                logger.error(f"[NOTA-TRASMISSIONE] ❌ Failed to trigger Nota Trasmissione import service for {sal_name}")

        except Exception as e:
            logger.error(f"[NOTA-TRASMISSIONE] Error triggering Nota Trasmissione import service: {e}")

    def launch_azure_container_passaggi_livello(self, sal_name):
        """Trigger Passaggi di Livello PDF import service via Service Bus"""
        logger.info(f"[PASSAGGI-LIVELLO] Triggering Passaggi di Livello PDF import service for: {sal_name}")

        try:
            import uuid

            # Prepare message for Passaggi di Livello import service
            message_data = {
                'task': 'passaggi-livello-import',
                'sal_storage_name': sal_name,
                'request_id': str(uuid.uuid4())
            }

            logger.info(f"[PASSAGGI-LIVELLO] Request ID: {message_data['request_id']}")

            # Send message to Passaggi di Livello import service queue
            PASSAGGI_LIVELLO_QUEUE = os.getenv('PASSAGGI_LIVELLO_SERVICE_QUEUE', 'import-sal-passaggi-livello')
            success = self.orchestrator.utils.send_service_message(
                PASSAGGI_LIVELLO_QUEUE,
                message_data
            )

            if success:
                logger.info(f"[PASSAGGI-LIVELLO] ✅ Passaggi di Livello import service triggered for {sal_name}")
            else:
                logger.error(f"[PASSAGGI-LIVELLO] ❌ Failed to trigger Passaggi di Livello import service for {sal_name}")

        except Exception as e:
            logger.error(f"[PASSAGGI-LIVELLO] Error triggering Passaggi di Livello import service: {e}")

    def launch_azure_container_timesheet(self, sal_name):
        """Trigger Timesheet PDF import service via Service Bus"""
        logger.info(f"[TIMESHEET] Triggering Timesheet PDF import service for: {sal_name}")

        try:
            import uuid

            # Prepare message for Timesheet import service
            message_data = {
                'task': 'timesheet-import',
                'sal_storage_name': sal_name,
                'request_id': str(uuid.uuid4())
            }

            logger.info(f"[TIMESHEET] Request ID: {message_data['request_id']}")

            # Send message to Timesheet import service queue
            TIMESHEET_QUEUE = os.getenv('TIMESHEET_SERVICE_QUEUE', 'import-sal-timesheet')
            success = self.orchestrator.utils.send_service_message(
                TIMESHEET_QUEUE,
                message_data
            )

            if success:
                logger.info(f"[TIMESHEET] ✅ Timesheet import service triggered for {sal_name}")
            else:
                logger.error(f"[TIMESHEET] ❌ Failed to trigger Timesheet import service for {sal_name}")

        except Exception as e:
            logger.error(f"[TIMESHEET] Error triggering Timesheet import service: {e}")

    def launch_azure_container_vap(self, sal_name):
        """Trigger VAP PDF import service via Service Bus"""
        logger.info(f"[VAP] Triggering VAP PDF import service for: {sal_name}")

        try:
            import uuid

            # Prepare message for VAP import service
            message_data = {
                'task': 'vap-import',
                'sal_storage_name': sal_name,
                'request_id': str(uuid.uuid4())
            }

            logger.info(f"[VAP] Request ID: {message_data['request_id']}")

            # Send message to VAP import service queue
            VAP_QUEUE = os.getenv('VAP_SERVICE_QUEUE', 'import-sal-vap')
            success = self.orchestrator.utils.send_service_message(
                VAP_QUEUE,
                message_data
            )

            if success:
                logger.info(f"[VAP] ✅ VAP import service triggered for {sal_name}")
            else:
                logger.error(f"[VAP] ❌ Failed to trigger VAP import service for {sal_name}")

        except Exception as e:
            logger.error(f"[VAP] Error triggering VAP import service: {e}")

    def launch_azure_container_pdf(self, sal):
        """Mark import as completed - PDF processing is the final step"""
        logger.info(f"[IMPORT] ✅ IMPORT COMPLETED SUCCESSFULLY for {sal}")
        current_import = self.orchestrator.active_imports.get(sal)
        if current_import is None:
            logger.warning(f"[IMPORT] No active import entry found for {sal} - creating minimal tracking stub to avoid KeyError")
            self.orchestrator.active_imports[sal] = {
                'current_step': 'completed',
                'status': 'completed',
                'data': {},
                'request_id': '',
                'db_backup_path': self.orchestrator.db_backup_paths.get(sal),
                'container_backup_paths': self.orchestrator.container_backup_paths.get(sal),
                'start_time': datetime.now().isoformat() if hasattr(datetime, 'now') else ''
            }
            current_import = self.orchestrator.active_imports[sal]
        else:
            current_import['status'] = 'completed'
            current_import['current_step'] = 'completed'
        self.orchestrator.utils.send_notification(sal, "Import", "Completato con successo")

        # Estrai il numero del SAL dal nome (es. "1_SAL_31_12_2022" → 1)
        try:
            sal_number = int(sal.split('_')[0])
            logger.info(f"[IMPORT] SAL number extracted: {sal_number}")

            # Mapping dei SAL successivi
            sal_mapping = {
                1: "2_SAL_30_04_2023",
                2: "3_SAL_31_08_2023",
                3: "4_SAL_31_12_2023",
                4: "5_SAL_30_04_2024",
                5: "6_SAL_31_08_2024",
                6: "7_SAL_31_12_2024",
                7: "8_SAL_30_04_2025",
                8: None  # No SAL after 8
            }

            next_sal = sal_mapping.get(sal_number)

            if next_sal is None:
                logger.info(f"[IMPORT] SAL {sal_number} is the last one (no further processing)")
                return

            logger.info(f"[IMPORT] Processing next SAL: {next_sal} after SAL {sal_number}")

            # Get previous request_id safely
            prev_request_id = current_import.get('request_id') if current_import else ''

            if sal_number <= 5:
                # SAL 1-5: restart from AQ phase
                logger.info(f"[IMPORT] SAL {sal_number} (1-5 range): Restarting {next_sal} from AQ phase")
                # Initialize active_imports entry for next SAL
                self.orchestrator.active_imports[next_sal] = {
                    'current_step': 'aq-import',
                    'status': 'running',
                    'data': current_import if current_import else {},
                    'request_id': prev_request_id or '',
                    'db_backup_path': None,
                    'container_backup_paths': None,
                    'start_time': datetime.now().isoformat() if hasattr(datetime, 'now') else ''
                }
                logger.info(f"[IMPORT] Launching AQ import for {next_sal}...")
                self.launch_azure_container_aq(next_sal)

            elif 6 <= sal_number <= 8:
                # SAL 6-8: restart from beginning (SFTP)
                logger.info(f"[IMPORT] SAL {sal_number} (6-8 range): Restarting {next_sal} from SFTP phase")

                # Prepare the data for the next SAL with SFTP starting point
                next_sal_data = {
                    'task': 'sftp-verify-copy',
                    'sal_storage_name': next_sal,
                    'request_id': prev_request_id or ''
                }

                # Initialize active_imports entry for next SAL
                self.orchestrator.active_imports[next_sal] = {
                    'current_step': 'sftp',
                    'status': 'running',
                    'data': next_sal_data,
                    'request_id': next_sal_data.get('request_id', ''),
                    'db_backup_path': None,
                    'container_backup_paths': None,
                    'start_time': datetime.now().isoformat() if hasattr(datetime, 'now') else ''
                }
                logger.info(f"[IMPORT] Triggering SFTP service for {next_sal}...")
                self.trigger_sftp_service(next_sal_data)
            else:
                logger.warning(f"[IMPORT] SAL {sal_number} is greater than 8 (no further processing)")

        except (ValueError, IndexError) as e:
            logger.error(f"[IMPORT] Error extracting SAL number from {sal}: {e}")
            logger.info(f"[IMPORT] Stopping SAL processing chain")

    def launch_db_rollback_sal(self, sal_name, sal_number):
        """Trigger DB rollback SAL service via Service Bus"""
        logger.info(f"[DB-ROLLBACK-SAL] Triggering DB rollback SAL service for: {sal_name}")
        logger.info(f"[DB-ROLLBACK-SAL] SAL Number: {sal_number}")
        
        try:
            import uuid

            # Prepare message for DB rollback SAL service
            message_data = {
                'task': 'db-rollback-sal',
                'sal_storage_name': sal_name,
                'sal_number': sal_number,
                'request_id': str(uuid.uuid4())
            }

            logger.info(f"[DB-ROLLBACK-SAL] Request ID: {message_data['request_id']}")

            # Send message to DB rollback SAL service queue
            DB_ROLLBACK_SAL_QUEUE = os.getenv('DB_ROLLBACK_SAL_SERVICE_QUEUE', 'import-sal-db-rollback-sal')
            success = self.orchestrator.utils.send_service_message(
                DB_ROLLBACK_SAL_QUEUE,
                message_data
            )
            
            if success:
                logger.info(f"[DB-ROLLBACK-SAL] ✅ DB rollback SAL service triggered for {sal_name}")
            else:
                logger.error(f"[DB-ROLLBACK-SAL] ❌ Failed to trigger DB rollback SAL service for {sal_name}")

        except Exception as e:
            logger.error(f"[DB-ROLLBACK-SAL] Error triggering DB rollback SAL service: {e}")

    def launch_db_rollback(self, sal_name, backup_path):
        """Trigger DB rollback service via Service Bus"""
        logger.info(f"[DB-ROLLBACK] Triggering DB rollback service for: {sal_name}")
        logger.info(f"[DB-ROLLBACK] Backup path: {backup_path}")
        
        try:
            import uuid

            # Prepare message for DB rollback service
            message_data = {
                'task': 'db-rollback',
                'sal_storage_name': sal_name,
                'backup_path': backup_path,
                'request_id': str(uuid.uuid4())
            }

            logger.info(f"[DB-ROLLBACK] Request ID: {message_data['request_id']}")

            # Send message to DB rollback service queue
            DB_ROLLBACK_QUEUE = os.getenv('DB_ROLLBACK_SERVICE_QUEUE', 'import-sal-db-rollback')
            success = self.orchestrator.utils.send_service_message(
                DB_ROLLBACK_QUEUE,
                message_data
            )
            
            if success:
                logger.info(f"[DB-ROLLBACK] ✅ DB rollback service triggered for {sal_name}")
            else:
                logger.error(f"[DB-ROLLBACK] ❌ Failed to trigger DB rollback service for {sal_name}")

        except Exception as e:
            logger.error(f"[DB-ROLLBACK] Error triggering DB rollback service: {e}")

    def launch_storage_cleanup(self, sal_name, reason):
        """Trigger storage cleanup service via Service Bus"""
        logger.info(f"[STORAGE-CLEANUP] Triggering storage cleanup service for: {sal_name}")
        logger.info(f"[STORAGE-CLEANUP] Reason: {reason}")

        try:
            import uuid

            # Prepare message for storage cleanup service
            message_data = {
                'task': 'storage-cleanup',
                'sal_storage_name': sal_name,
                'cleanup_reason': reason,
                'request_id': str(uuid.uuid4())
            }

            logger.info(f"[STORAGE-CLEANUP] Request ID: {message_data['request_id']}")

            # Send message to storage cleanup service queue
            STORAGE_CLEANUP_QUEUE = os.getenv('STORAGE_CLEANUP_SERVICE_QUEUE', 'import-sal-storage-cleanup')
            success = self.orchestrator.utils.send_service_message(
                STORAGE_CLEANUP_QUEUE,
                message_data
            )

            if success:
                logger.info(f"[STORAGE-CLEANUP] ✅ Storage cleanup service triggered for {sal_name}")
            else:
                logger.error(f"[STORAGE-CLEANUP] ❌ Failed to trigger storage cleanup service for {sal_name}")

        except Exception as e:
            logger.error(f"[STORAGE-CLEANUP] Error triggering storage cleanup service: {e}")
