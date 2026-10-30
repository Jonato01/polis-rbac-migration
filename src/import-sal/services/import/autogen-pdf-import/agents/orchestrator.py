"""
AutoGen Orchestrator
Coordina il flusso di elaborazione documenti usando AutoGen multi-agent
"""
import logging
from typing import Optional

import autogen
from autogen import AssistantAgent, UserProxyAgent, GroupChat, GroupChatManager

from config import (
    CLAUDE_API_KEY,
    CLAUDE_BASE_URL,
    CLAUDE_MODEL
)
from config.document_types import load_document_types_from_db

logger = logging.getLogger(__name__)


class AutoGenOrchestrator:
    """
    Orchestratore AutoGen per coordinare gli agent di import
    """

    def __init__(self):
        self.llm_config = {
            "config_list": [{
                "model": CLAUDE_MODEL,
                "api_key": CLAUDE_API_KEY,
                "base_url": CLAUDE_BASE_URL
            }],
            "temperature": 0.1,
            "timeout": 120
        }

        self._setup_agents()

    def _setup_agents(self):
        """
        Configura gli agent AutoGen
        """
        # Carica configurazione tipi documento dal database
        doc_types_config = load_document_types_from_db()

        # Costruisci lista categorie dinamicamente
        categories_list = []
        hints_list = []

        for doc_type, config in doc_types_config.items():
            name = config.get("name", doc_type)
            description = config.get("description", "")
            categories_list.append(f"- {doc_type}: {name} ({description})")

            # Aggiungi hints per riconoscimento
            hints = config.get("classification_hints", [])
            if hints:
                hints_str = ", ".join(f'"{h}"' for h in hints[:3])
                hints_list.append(f"- {hints_str} → {doc_type}")

        categories_list.append("- UNKNOWN: Non riconoscibile")

        categories_text = "\n".join(categories_list)
        hints_text = "\n".join(hints_list) if hints_list else ""

        hints_section = f"\n\nIMPORTANTE - Indizi dal nome file:\n{hints_text}\n" if hints_text else ""

        # Classifier Agent - classifica i documenti (DINAMICO DA DB)
        self.classifier_agent = AssistantAgent(
            name="ClassifierAgent",
            system_message=f"""Sei un esperto classificatore di documenti aziendali italiani.
Il tuo compito è analizzare il contenuto OCR e il nome file per determinare il tipo di documento.

Categorie disponibili:
{categories_text}{hints_section}
Rispondi sempre in formato JSON con: categoria, confidence (0-1), motivo.""",
            llm_config=self.llm_config
        )

        # Router Agent - smista ai giusti importer
        self.router_agent = AssistantAgent(
            name="RouterAgent",
            system_message="""Sei un router che smista i documenti agli agent specializzati.
Ricevi la classificazione dal ClassifierAgent e determini quale importer usare.
Coordini il flusso: classificazione -> estrazione -> validazione -> import.""",
            llm_config=self.llm_config
        )

        # DB Writer Agent - scrive nel database
        self.db_writer_agent = AssistantAgent(
            name="DBWriterAgent",
            system_message="""Sei un esperto di database MySQL.
Il tuo compito è:
1. Ricevere dati JSON strutturati
2. Generare query INSERT/UPSERT corrette
3. Gestire errori e proporre correzioni
4. Validare i dati prima dell'inserimento

Rispetta sempre i vincoli FK e i tipi di dato delle colonne.""",
            llm_config=self.llm_config
        )

        # User Proxy per esecuzione codice
        self.user_proxy = UserProxyAgent(
            name="UserProxy",
            human_input_mode="NEVER",
            max_consecutive_auto_reply=10,
            code_execution_config={"use_docker": False}
        )

    def create_group_chat(self) -> GroupChat:
        """
        Crea un GroupChat per la conversazione tra agent
        """
        return GroupChat(
            agents=[
                self.user_proxy,
                self.classifier_agent,
                self.router_agent,
                self.db_writer_agent
            ],
            messages=[],
            max_round=10
        )

    async def process_document(
        self,
        ocr_content: str,
        filename: str,
        pdf_url: str,
        sal_number: int
    ) -> dict:
        """
        Elabora un singolo documento attraverso il flusso AutoGen

        Args:
            ocr_content: Contenuto OCR del documento
            filename: Nome del file
            pdf_url: URL del PDF su blob storage
            sal_number: Numero SAL

        Returns:
            Dict con risultato elaborazione
        """
        logger.info(f"Orchestrator: elaboro {filename}")

        # Per ora utilizziamo l'approccio diretto senza GroupChat
        # per maggiore controllo e performance

        # 1. Classificazione
        from services.claude_service import ClaudeService
        claude_service = ClaudeService()

        classification = await claude_service.classify_document(ocr_content, filename)
        doc_type = classification.get("categoria", "UNKNOWN")
        confidence = classification.get("confidence", 0)

        logger.info(f"Classificato come {doc_type} (confidence: {confidence})")

        if doc_type == "UNKNOWN":
            return {
                "file": filename,
                "status": "skipped",
                "document_type": "UNKNOWN",
                "confidence": confidence,
                "reason": classification.get("motivo", "Non classificabile")
            }

        # Verifica che il tipo sia configurato
        doc_types_config = load_document_types_from_db()
        if doc_type not in doc_types_config:
            return {
                "file": filename,
                "status": "skipped",
                "document_type": "UNKNOWN",
                "confidence": confidence,
                "reason": classification.get("motivo", "Non classificabile")
            }

        # 2. Estrazione con agent specializzato
        from agents.importers import get_importer
        importer = get_importer(doc_type)

        if not importer:
            logger.error(f"Importer non trovato per {doc_type}")
            return {
                "file": filename,
                "status": "error",
                "document_type": doc_type,
                "error": f"Importer non implementato per {doc_type}"
            }

        # 3. Estrazione dati
        json_data = await importer.extract(ocr_content, filename, pdf_url)

        if not json_data:
            return {
                "file": filename,
                "status": "failed",
                "document_type": doc_type,
                "error": "Estrazione dati fallita"
            }

        # 4. Import nel database
        success, error = await importer.import_to_db(json_data, sal_number, pdf_url)

        if success:
            return {
                "file": filename,
                "status": "success",
                "document_type": doc_type,
                "confidence": confidence
            }
        else:
            return {
                "file": filename,
                "status": "failed",
                "document_type": doc_type,
                "error": error
            }

