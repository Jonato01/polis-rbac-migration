"""
Job Service
Gestisce il coordinamento job multi-replica tramite MySQL.
Ogni job corrisponde a un import di una cartella SAL distribuito su piu' worker.
"""
import logging
import time
from typing import Optional

from services.db_service import _get_conn

logger = logging.getLogger(__name__)


class JobService:
    """
    Servizio per operazioni CRUD sulla tabella autogen_import_jobs.
    Con autocommit=True, i singoli statement (create_job, mark_*) non necessitano
    di transazioni esplicite. report_batch_completion e close_stale_jobs usano
    start_transaction() per garantire atomicita' multi-replica.
    """

    def create_job(
        self,
        job_id: str,
        folder_path: str,
        sal_number: int,
        total_files: int,
        total_batches: int,
        force_reprocess: bool = False,
        upload_id: int = None,
        retry_count: int = 0,
        retry_group_id: str = None
    ) -> bool:
        """Crea un nuovo job di import."""
        # Se retry_group_id non specificato, questo e' il job root della catena
        if retry_group_id is None:
            retry_group_id = job_id

        try:
            with _get_conn() as (conn, cursor):
                cursor.execute(
                    """INSERT INTO autogen_import_jobs
                       (job_id, folder_path, sal_number, total_files, total_batches,
                        force_reprocess, status, upload_id,
                        retry_count, retry_group_id)
                       VALUES (%s, %s, %s, %s, %s, %s, 'dispatching', %s, %s, %s)""",
                    (job_id, folder_path, sal_number, total_files, total_batches,
                     1 if force_reprocess else 0, upload_id,
                     retry_count, retry_group_id)
                )
            logger.info(
                f"Job {job_id} creato: {folder_path}, "
                f"{total_files} file, {total_batches} batch"
                f"{f' (retry {retry_count})' if retry_count > 0 else ''}"
            )
            return True
        except Exception as e:
            logger.exception(f"Errore creazione job {job_id}: {e}")
            return False

    def mark_job_processing(self, job_id: str) -> bool:
        """Aggiorna lo status del job a 'processing'"""
        try:
            with _get_conn() as (conn, cursor):
                cursor.execute(
                    "UPDATE autogen_import_jobs SET status = 'processing' WHERE job_id = %s",
                    (job_id,)
                )
            return True
        except Exception as e:
            logger.exception(f"Errore aggiornamento job {job_id}: {e}")
            return False

    def report_batch_completion(
        self,
        job_id: str,
        batch_results: list[dict],
        max_retries: int = 5
    ) -> Optional[dict]:
        """
        Aggiorna atomicamente i contatori del job dopo il completamento di un batch.
        Salva solo contatori numerici, niente JSON dettagliato.
        """
        success = sum(1 for r in batch_results if r.get("status") == "success")
        failed = sum(1 for r in batch_results if r.get("status") == "failed")
        skipped = sum(1 for r in batch_results if r.get("status") == "skipped")
        errors = sum(1 for r in batch_results if r.get("status") == "error")

        for attempt in range(1, max_retries + 1):
            try:
                with _get_conn(dictionary=True) as (conn, cursor):
                    conn.start_transaction()

                    try:
                        cursor.execute(
                            """UPDATE autogen_import_jobs
                               SET completed_batches = completed_batches + 1,
                                   success_count = success_count + %s,
                                   failed_count = failed_count + %s,
                                   skipped_count = skipped_count + %s,
                                   error_count = error_count + %s,
                                   status = IF(completed_batches + 1 >= total_batches, 'completed', status),
                                   completed_at = IF(completed_batches + 1 >= total_batches, NOW(), completed_at)
                               WHERE job_id = %s""",
                            (success, failed, skipped, errors, job_id)
                        )

                        cursor.execute(
                            """SELECT job_id, folder_path, sal_number, total_files, total_batches,
                                      completed_batches, success_count, failed_count,
                                      skipped_count, error_count, status
                               FROM autogen_import_jobs
                               WHERE job_id = %s""",
                            (job_id,)
                        )
                        job = cursor.fetchone()

                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise

                if not job:
                    logger.error(f"Job {job_id} non trovato")
                    return None

                is_last = job['status'] == 'completed'

                logger.info(
                    f"Job {job_id}: batch completato "
                    f"({job['completed_batches']}/{job['total_batches']}), "
                    f"success={job['success_count']}, failed={job['failed_count']}"
                )

                if is_last:
                    logger.info(f"Job {job_id}: TUTTI I BATCH COMPLETATI")
                    return {
                        "job_id": job['job_id'],
                        "folder_path": job['folder_path'],
                        "sal_number": job['sal_number'],
                        "total_files": job['total_files'],
                        "success_count": job['success_count'],
                        "failed_count": job['failed_count'],
                        "skipped_count": job['skipped_count'],
                        "error_count": job['error_count']
                    }

                return None

            except Exception as e:
                logger.exception(
                    f"Job {job_id}: report_batch_completion FALLITO "
                    f"(tentativo {attempt}/{max_retries}): {e}"
                )
                if attempt == max_retries:
                    return None
                time.sleep(0.5 * attempt)

        return None

    def get_processing_jobs(self) -> list[dict]:
        """Ritorna dati dei job in 'processing' (inclusi campi per retry)."""
        try:
            with _get_conn(dictionary=True) as (conn, cursor):
                cursor.execute(
                    """SELECT job_id, completed_batches, folder_path, sal_number,
                              upload_id, retry_group_id, retry_count, force_reprocess
                       FROM autogen_import_jobs WHERE status = 'processing'"""
                )
                return cursor.fetchall()
        except Exception as e:
            logger.exception(f"Errore get_processing_jobs: {e}")
            return []

    def close_stale_jobs(self, job_ids: list[str]) -> list[dict]:
        """
        Chiude job stuck. FOR UPDATE per evitare race condition tra repliche.
        Ri-controlla status='processing' nel SELECT per sicurezza.
        """
        if not job_ids:
            return []

        closed_jobs = []
        try:
            with _get_conn(dictionary=True) as (conn, cursor):
                conn.start_transaction()

                try:
                    placeholders = ','.join(['%s'] * len(job_ids))
                    cursor.execute(
                        f"""SELECT job_id, folder_path, sal_number, total_files, total_batches,
                                   completed_batches, success_count, failed_count,
                                   skipped_count, error_count, upload_id,
                                   retry_group_id, retry_count, force_reprocess
                            FROM autogen_import_jobs
                            WHERE job_id IN ({placeholders}) AND status = 'processing'
                            FOR UPDATE""",
                        tuple(job_ids)
                    )
                    jobs = cursor.fetchall()

                    for job in jobs:
                        cursor.execute(
                            "UPDATE autogen_import_jobs SET status = 'completed', completed_at = NOW() WHERE job_id = %s",
                            (job['job_id'],)
                        )

                        closed_jobs.append({
                            "job_id": job['job_id'],
                            "folder_path": job['folder_path'],
                            "sal_number": job['sal_number'],
                            "upload_id": job['upload_id'],
                            "total_files": job['total_files'],
                            "total_batches": job['total_batches'],
                            "completed_batches": job['completed_batches'],
                            "success_count": job['success_count'],
                            "failed_count": job['failed_count'],
                            "skipped_count": job['skipped_count'],
                            "error_count": job['error_count'],
                            "retry_group_id": job['retry_group_id'],
                            "retry_count": job['retry_count'],
                            "force_reprocess": job['force_reprocess']
                        })

                        logger.warning(
                            f"Job {job['job_id']} stuck, chiusura forzata "
                            f"({job['completed_batches']}/{job['total_batches']} batch completati)"
                        )

                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

            return closed_jobs

        except Exception as e:
            logger.exception(f"Errore close_stale_jobs: {e}")
            return []

    def get_job_by_id(self, job_id: str) -> Optional[dict]:
        """Ritorna dati di un singolo job (retry_count, retry_group_id, upload_id)."""
        try:
            with _get_conn(dictionary=True) as (conn, cursor):
                cursor.execute(
                    """SELECT job_id, retry_count, retry_group_id, upload_id,
                              folder_path, sal_number, force_reprocess
                       FROM autogen_import_jobs WHERE job_id = %s""",
                    (job_id,)
                )
                return cursor.fetchone()
        except Exception as e:
            logger.exception(f"Errore get_job_by_id {job_id}: {e}")
            return None

    def get_retry_chain_results(self, retry_group_id: str) -> dict:
        """
        Aggrega risultati di tutti i job nella catena di retry.
        Usa solo i contatori numerici, senza caricare batch_results (JSON enorme
        che causava MySQL Out of sort memory).
        """
        try:
            with _get_conn(dictionary=True) as (conn, cursor):
                cursor.execute(
                    """SELECT success_count, failed_count, skipped_count,
                              error_count, upload_id, folder_path,
                              sal_number, retry_count
                       FROM autogen_import_jobs
                       WHERE retry_group_id = %s
                       ORDER BY retry_count ASC""",
                    (retry_group_id,)
                )
                jobs = cursor.fetchall()

            if not jobs:
                return {
                    "success_count": 0, "failed_count": 0,
                    "skipped_count": 0, "error_count": 0,
                    "all_results": [], "upload_id": None
                }

            total_success = sum(j['success_count'] for j in jobs)
            total_failed = sum(j['failed_count'] for j in jobs)
            total_skipped = sum(j['skipped_count'] for j in jobs)
            total_errors = sum(j['error_count'] for j in jobs)

            root_job = jobs[0]

            logger.info(
                f"Retry chain {retry_group_id}: {len(jobs)} job, "
                f"success={total_success}, failed={total_failed}, "
                f"skipped={total_skipped}, errors={total_errors}"
            )

            return {
                "success_count": total_success,
                "failed_count": total_failed,
                "skipped_count": total_skipped,
                "error_count": total_errors,
                "all_results": [],
                "upload_id": root_job['upload_id'],
                "folder_path": root_job['folder_path'],
                "sal_number": root_job['sal_number']
            }

        except Exception as e:
            logger.exception(f"Errore get_retry_chain_results {retry_group_id}: {e}")
            return {
                "success_count": 0, "failed_count": 0,
                "skipped_count": 0, "error_count": 0,
                "all_results": [], "upload_id": None
            }

    def mark_job_failed(self, job_id: str, error: str) -> bool:
        """Segna il job come fallito"""
        try:
            with _get_conn() as (conn, cursor):
                cursor.execute(
                    """UPDATE autogen_import_jobs
                       SET status = 'failed', completed_at = NOW()
                       WHERE job_id = %s""",
                    (job_id,)
                )
            logger.error(f"Job {job_id} marcato come fallito: {error}")
            return True
        except Exception as e:
            logger.exception(f"Errore marcatura fallimento job {job_id}: {e}")
            return False
