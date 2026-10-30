"""
OpenAI Service
Gestisce chiamate ad Azure OpenAI per classificazione e estrazione dati
"""
import json
import logging
from typing import Optional

from openai import AsyncAzureOpenAI

from config import (
    AZURE_OPENAI_ENDPOINT,
    AZURE_OPENAI_KEY,
    AZURE_OPENAI_DEPLOYMENT,
    AZURE_OPENAI_API_VERSION,
    CONFIDENCE_THRESHOLD
)
from config.document_types import load_document_types_from_db, get_classification_hints
from services.rate_limiter import openai_limiter

logger = logging.getLogger(__name__)


class OpenAIService:
    """
    Servizio per interazione con Azure OpenAI
    """

    def __init__(self):
        self.client = AsyncAzureOpenAI(
            azure_endpoint=AZURE_OPENAI_ENDPOINT,
            api_key=AZURE_OPENAI_KEY,
            api_version=AZURE_OPENAI_API_VERSION
        )
        self.deployment = AZURE_OPENAI_DEPLOYMENT

    def _build_classification_prompt(self) -> str:
        """
        Costruisce dinamicamente il prompt di classificazione
        dalla configurazione dei tipi documento (caricata da DB)
        """
        # Carica configurazione dal database
        doc_types_config = load_document_types_from_db()

        # Costruisci hints dal nome file
        hints_section = "IMPORTANTE: Il nome del file spesso contiene indizi sulla tipologia:\n"
        for doc_type, config in doc_types_config.items():
            hints = config.get("classification_hints", [])
            if hints:
                hints_str = ", ".join(f'"{h}"' for h in hints[:3])
                hints_section += f'- {hints_str} -> {doc_type}\n'

        # Costruisci lista categorie CON negative hints
        categories_section = "Classifica in UNA delle seguenti categorie:\n"
        for doc_type, config in doc_types_config.items():
            name = config.get("name", doc_type)
            description = config.get("description", "")
            categories_section += f"- {doc_type}: {name} ({description})\n"
            # Aggiungi negative hints per escludere falsi positivi
            negative_hints = config.get("negative_hints", [])
            if negative_hints:
                hints_str = ", ".join(f'"{h}"' for h in negative_hints)
                categories_section += f"  NON è {doc_type} se contiene: {hints_str}\n"
        categories_section += "- UNKNOWN: Se non riconoscibile o non corrisponde a nessuna categoria\n"

        # Regole esplicite per evitare falsi positivi
        rules_section = """
REGOLE IMPORTANTI:
1. Se il documento NON corrisponde CHIARAMENTE a una delle categorie sopra, usa UNKNOWN
2. Usa confidence >= 0.8 solo se sei CERTO della classificazione
3. Documenti generici (prospetti, elenchi, riepiloghi) senza riferimenti specifici = UNKNOWN
4. Se hai dubbi, usa UNKNOWN con confidence bassa

ESEMPI DI DOCUMENTI UNKNOWN (non classificabili):
- Prospetti costo del personale generici
- Elenchi generici dipendenti
- Report statistici generici
- Documenti amministrativi senza categoria specifica
- Qualsiasi documento che non sia chiaramente una delle categorie sopra"""

        return f"""{hints_section}

{categories_section}

{rules_section}"""

    async def classify_document(
        self,
        ocr_content: str,
        filename: str
    ) -> dict:
        """
        Classifica un documento in una delle categorie supportate
        Le categorie sono generate dinamicamente dalla configurazione

        Args:
            ocr_content: Contenuto OCR del documento
            filename: Nome del file (usato come hint)

        Returns:
            Dict con categoria, confidence e motivo
        """
        await openai_limiter.acquire()

        # Costruisci prompt dinamico dalla configurazione
        dynamic_categories = self._build_classification_prompt()

        prompt = f"""Analizza il NOME DEL FILE e il contenuto OCR per classificare il documento.

NOME FILE: {filename}
CONTENUTO OCR (prime pagine):
{ocr_content[:3000]}

{dynamic_categories}

Rispondi in JSON:
{{"categoria": "CODICE", "confidence": 0.0-1.0, "motivo": "breve spiegazione"}}"""

        try:
            response = await self.client.chat.completions.create(
                model=self.deployment,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.1
            )

            result = json.loads(response.choices[0].message.content)

            # Se bassa confidence, forza UNKNOWN invece di procedere con classificazione dubbia
            if result.get("confidence", 0) < CONFIDENCE_THRESHOLD:
                original_categoria = result.get("categoria", "UNKNOWN")
                original_confidence = result.get("confidence", 0)
                logger.warning(
                    f"BASSA CONFIDENCE: {original_confidence:.0%} per categoria "
                    f"{original_categoria}. Soglia: {CONFIDENCE_THRESHOLD:.0%}. "
                    f"Forzo UNKNOWN per evitare classificazione errata."
                )
                return {
                    "categoria": "UNKNOWN",
                    "confidence": original_confidence,
                    "motivo": f"Confidence troppo bassa ({original_confidence:.0%} < {CONFIDENCE_THRESHOLD:.0%}). "
                              f"Categoria originale suggerita: {original_categoria}. "
                              f"Motivo originale: {result.get('motivo', 'N/A')}"
                }

            return result

        except Exception as e:
            logger.error(f"Errore classificazione: {e}")
            return {"categoria": "UNKNOWN", "confidence": 0, "motivo": str(e)}

    async def extract_data(
        self,
        ocr_content: str,
        filename: str,
        doc_type: str,
        extraction_prompt: str
    ) -> dict:
        """
        Estrae dati strutturati da un documento

        Args:
            ocr_content: Contenuto OCR del documento
            filename: Nome del file
            doc_type: Tipo documento
            extraction_prompt: Prompt specifico per l'estrazione

        Returns:
            Dict con dati estratti
        """
        await openai_limiter.acquire()

        full_prompt = f"""Analizza il seguente documento di tipo {doc_type}.

NOME FILE: {filename}
CONTENUTO OCR:
{ocr_content}

{extraction_prompt}

IMPORTANTE: 
- Se un dato non è presente o non chiaro, usa null
- I numeri devono essere senza separatore delle migliaia (usa . come decimale)
- Le date devono essere in formato YYYY-MM-DD
- Estrai codici (CIG, CUP, Protocollo) esattamente come appaiono
- Se il nome file contiene dati non trovati nell'OCR, usa quelli del nome file

Rispondi SOLO con il JSON richiesto, senza spiegazioni."""

        try:
            response = await self.client.chat.completions.create(
                model=self.deployment,
                messages=[{"role": "user", "content": full_prompt}],
                response_format={"type": "json_object"},
                temperature=0.1
            )

            result = json.loads(response.choices[0].message.content)
            logger.info(f"Dati estratti per {doc_type}: {len(result)} campi")
            return result

        except Exception as e:
            logger.error(f"Errore estrazione dati: {e}")
            return {}

    async def fix_sql_query(
        self,
        original_query: str,
        error_message: str,
        json_data: dict,
        doc_type: str
    ) -> Optional[str]:
        """
        Usa GPT per correggere una query SQL fallita

        Args:
            original_query: Query che ha causato errore
            error_message: Messaggio di errore MySQL
            json_data: Dati estratti originali
            doc_type: Tipo documento

        Returns:
            Query corretta o None se non riesce
        """
        await openai_limiter.acquire()

        prompt = f"""La seguente query SQL per un documento {doc_type} è fallita.

ERRORE MySQL:
{error_message}

QUERY ORIGINALE:
{original_query[:2000]}

DATI JSON ESTRATTI:
{json.dumps(json_data, indent=2, ensure_ascii=False)[:2000]}

Analizza l'errore e genera una query corretta che risolva il problema.
Possibili cause comuni:
- Valori troppo lunghi per il campo (tronca)
- Formato data errato (usa YYYY-MM-DD)
- Caratteri speciali non escaped
- Violazione vincolo FK (usa NULL)
- Valore numerico non valido (usa NULL o 0)

Rispondi SOLO con la query SQL corretta, senza spiegazioni."""

        try:
            response = await self.client.chat.completions.create(
                model=self.deployment,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1
            )

            corrected_query = response.choices[0].message.content.strip()

            # Rimuovi eventuale markdown
            if corrected_query.startswith("```"):
                corrected_query = corrected_query.split("```")[1]
                if corrected_query.startswith("sql"):
                    corrected_query = corrected_query[3:]

            return corrected_query.strip() if corrected_query else None

        except Exception as e:
            logger.error(f"Errore correzione query: {e}")
            return None

