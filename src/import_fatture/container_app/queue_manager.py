"""
Gestione Azure Service Bus: ricezione messaggi di import e invio risultati.
"""
import logging
from typing import Callable

from azure.servicebus import ServiceBusClient, ServiceBusMessage
from azure.servicebus.exceptions import ServiceBusError

from container_app.azure_clients import get_servicebus_client
from container_app.config import (
    SERVICEBUS_INPUT_QUEUE,
    SERVICEBUS_OUTPUT_QUEUE,
)
from container_app.models import ImportRequest, ImportResponse

logger = logging.getLogger("container_app.queue_manager")


class QueueManager:
    def __init__(
        self,
        input_queue: str = SERVICEBUS_INPUT_QUEUE,
        output_queue: str = SERVICEBUS_OUTPUT_QUEUE,
        client: ServiceBusClient | None = None,
    ):
        self.input_queue = input_queue
        self.output_queue = output_queue
        self._client: ServiceBusClient | None = client

    # ------------------------------------------------------------------
    # Connessione
    # ------------------------------------------------------------------

    def _get_client(self) -> ServiceBusClient:
        if self._client is None:
            # Identity-first: AZURE_SERVICEBUS_NAMESPACE + managed identity,
            # fallback legacy AZURE_SERVICEBUS_CONNECTION_STRING (vedi azure_clients.py)
            self._client = get_servicebus_client(
                namespace_var="AZURE_SERVICEBUS_NAMESPACE",
                conn_str_var="AZURE_SERVICEBUS_CONNECTION_STRING",
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ------------------------------------------------------------------
    # Ricezione messaggi
    # ------------------------------------------------------------------

    def start_listening(
        self, callback: Callable[[ImportRequest], ImportResponse],
    ) -> None:
        """
        Loop infinito: ricevi messaggio -> processa -> rispondi -> completa.
        Se il messaggio e' malformato, viene spostato in dead-letter queue.
        """
        client = self._get_client()
        logger.info(
            "In ascolto sulla coda '%s'...", self.input_queue
        )

        with client.get_queue_receiver(
            queue_name=self.input_queue,
            max_wait_time=None,  # Attesa indefinita
        ) as receiver:
            for msg in receiver:
                try:
                    body = str(msg)
                    logger.info("Messaggio ricevuto: %s", body[:200])

                    request = ImportRequest.from_json(body)
                    logger.info(
                        "Elaborazione richiesta %s per cartella '%s'",
                        request.upload_id,
                        request.folder_path,
                    )

                    # Completa il messaggio subito (rimuovi dalla coda)
                    # cosi' non c'e' rischio di lock expiration durante
                    # l'elaborazione (che puo' richiedere diversi minuti)
                    receiver.complete_message(msg)
                    logger.info("Messaggio rimosso dalla coda")

                    # Processa
                    response = callback(request)

                    # Invia risultato
                    self.send_result(response)
                    logger.info(
                        "Richiesta %s completata con status=%s",
                        request.upload_id,
                        response.status.value,
                    )

                except (KeyError, ValueError) as e:
                    logger.error(
                        "Messaggio malformato, spostato in dead-letter: %s", e
                    )
                    receiver.dead_letter_message(
                        msg,
                        reason="Messaggio malformato",
                        error_description=str(e),
                    )

                except ServiceBusError as e:
                    logger.error("Errore Service Bus: %s", e)
                    # Il messaggio tornera' disponibile dopo il lock timeout

                except Exception as e:
                    logger.exception(
                        "Errore imprevisto durante elaborazione: %s", e
                    )
                    try:
                        receiver.abandon_message(msg)
                    except Exception:
                        pass

    # ------------------------------------------------------------------
    # Invio risultati
    # ------------------------------------------------------------------

    def send_result(self, response: ImportResponse) -> None:
        """Invia il risultato sulla coda di output."""
        client = self._get_client()
        with client.get_queue_sender(queue_name=self.output_queue) as sender:
            message = ServiceBusMessage(response.to_json())
            sender.send_messages(message)
            logger.info(
                "Risultato inviato per richiesta %s sulla coda '%s'",
                response.upload_id,
                self.output_queue,
            )
