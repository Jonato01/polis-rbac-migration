"""
Claude Service
Gestisce chiamate a Claude (Azure AI Foundry) per classificazione e estrazione dati
"""
import json
import re
import logging
from typing import Optional

from anthropic import AsyncAnthropic

from config import (
    CLAUDE_API_KEY,
    CLAUDE_BASE_URL,
    CLAUDE_MODEL,
    CONFIDENCE_THRESHOLD
)
from config.document_types import load_document_types_from_db, get_classification_hints
from services.rate_limiter import claude_limiter

logger = logging.getLogger(__name__)

# Classificazione sempre con Sonnet (veloce, sufficiente per riconoscere il tipo)
CLASSIFICATION_MODEL = "claude-sonnet-4-5"


class ClaudeService:
    """
    Servizio per interazione con Claude su Azure AI Foundry
    """

    def __init__(self):
        self.client = AsyncAnthropic(
            api_key=CLAUDE_API_KEY,
            base_url=CLAUDE_BASE_URL
        )
        self.model = CLAUDE_MODEL

    def _extract_json(self, text: str) -> dict:
        """
        Estrae JSON dalla risposta di Claude.
        Claude non ha response_format, quindi il JSON potrebbe essere
        avvolto in testo o markdown.
        Normalizza sempre il risultato a dict (gestisce il caso in cui
        Claude restituisce un array invece di un oggetto).
        """
        text = text.strip()
        result = None

        # Prova parse diretto
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            pass

        # Cerca blocco ```json ... ``` (anche senza chiusura ``` per risposte troncate)
        if result is None:
            json_block = re.search(r'```(?:json)?\s*\n?(.*?)(?:\n```|$)', text, re.DOTALL)
            if json_block:
                try:
                    result = json.loads(json_block.group(1).strip())
                except json.JSONDecodeError:
                    pass

        # Strip manuale se inizia con ```json
        if result is None and text.startswith("```"):
            stripped = re.sub(r'^```(?:json)?\s*\n?', '', text)
            stripped = re.sub(r'\n?```\s*$', '', stripped).strip()
            try:
                result = json.loads(stripped)
            except json.JSONDecodeError:
                pass

        # Cerca il primo { ... } o [ ... ] nel testo con tracking stringhe
        if result is None:
            for start_char, end_char in [('{', '}'), ('[', ']')]:
                start = text.find(start_char)
                if start == -1:
                    continue
                depth = 0
                in_string = False
                escape_next = False
                for i in range(start, len(text)):
                    c = text[i]
                    if escape_next:
                        escape_next = False
                        continue
                    if c == '\\':
                        escape_next = True
                        continue
                    if c == '"':
                        in_string = not in_string
                        continue
                    if in_string:
                        continue
                    if c == start_char:
                        depth += 1
                    elif c == end_char:
                        depth -= 1
                        if depth == 0:
                            try:
                                result = json.loads(text[start:i + 1])
                            except json.JSONDecodeError:
                                pass
                            break
                if result is not None:
                    break

        # Tentativo fix virgolette non escaped dentro valori stringa
        # Es: "campo": "testo con "virgolette" non escaped"
        if result is None:
            result = self._fix_unescaped_quotes_and_parse(text)

        # Tentativo riparazione JSON malformato/troncato
        if result is None:
            result = self._try_repair_json(text)

        if result is None:
            logger.error(f"Impossibile estrarre JSON dalla risposta (len={len(text)}): {text[:2000]}")
            return {}

        return self._normalize_to_dict(result)

    def _normalize_to_dict(self, result) -> dict:
        """
        Normalizza il risultato JSON a dict.
        Gestisce il caso in cui Claude restituisce un array di oggetti
        invece di un singolo oggetto (es. un oggetto per pagina).
        """
        if isinstance(result, dict):
            return result

        if isinstance(result, list):
            if len(result) == 0:
                return {}
            # Lista con un solo dict -> unwrap
            if len(result) == 1 and isinstance(result[0], dict):
                logger.info("JSON normalizzato: unwrap lista con 1 elemento")
                return result[0]
            # Lista di dict -> merge, preferendo valori non-null
            if all(isinstance(r, dict) for r in result):
                logger.warning(
                    f"JSON normalizzato: merge di {len(result)} oggetti in lista"
                )
                merged = {}
                for r in result:
                    for k, v in r.items():
                        if k not in merged or (v is not None and merged[k] is None):
                            merged[k] = v
                return merged

        logger.error(f"Tipo JSON inatteso: {type(result)}")
        return {}

    def _fix_unescaped_quotes_and_parse(self, text: str) -> dict | list | None:
        """
        Fix per risposte Claude con virgolette non escaped dentro valori stringa.
        Es: "campo": "testo con "virgolette" non escaped"
        Lavora riga per riga: identifica le righe con pattern "key": "value"
        e escapa le virgolette interne al valore.
        """
        # Strip markdown code fences
        cleaned = text.strip()
        if cleaned.startswith('```'):
            cleaned = re.sub(r'^```(?:json)?\s*\n?', '', cleaned)
            cleaned = re.sub(r'\n?```\s*$', '', cleaned).strip()

        lines = cleaned.split('\n')
        fixed_lines = []
        changed = False
        for line in lines:
            # Match JSON lines con valori stringa: "key": "value",
            # Il .+ greedy cattura tutto fino all'ultimo " prima di ,/}/fine riga
            match = re.match(r'^(\s*"[^"]+"\s*:\s*")(.+)("(?:,)?\s*)$', line)
            if match:
                prefix = match.group(1)
                value = match.group(2)
                suffix = match.group(3)
                if '"' in value:
                    # Proteggi virgolette gia' escaped, escapa le altre, ripristina
                    value = value.replace('\\"', '\x00')
                    value = value.replace('"', '\\"')
                    value = value.replace('\x00', '\\"')
                    changed = True
                fixed_lines.append(prefix + value + suffix)
            else:
                fixed_lines.append(line)

        if not changed:
            return None

        fixed = '\n'.join(fixed_lines)
        try:
            result = json.loads(fixed)
            logger.warning(f"JSON riparato (virgolette non escaped fixate)")
            return result
        except json.JSONDecodeError:
            return None

    def _try_repair_json(self, text: str) -> dict | list | None:
        """
        Tentativo di riparazione JSON malformato o troncato.
        Gestisce: trailing commas, risposte troncate da max_tokens,
        testo non-JSON mescolato.
        Usa uno stack per chiudere bracket/braces nell'ordine corretto
        (es. {..."righe":[{...  →  }]}).
        """
        # Trova il primo { o [
        start = -1
        for i, c in enumerate(text):
            if c in ('{', '['):
                start = i
                break
        if start == -1:
            return None

        candidate = text[start:]

        # Rimuovi trailing commas prima di } o ]
        candidate = re.sub(r',\s*([}\]])', r'\1', candidate)

        # Prova parse diretto dopo pulizia
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

        # Analizza struttura con STACK per tracciare ordine apertura bracket/braces
        stack = []  # stack di '{' e '['
        in_string = False
        escape_next = False
        last_comma_outside_string = -1

        for i, c in enumerate(candidate):
            if escape_next:
                escape_next = False
                continue
            if c == '\\' and in_string:
                escape_next = True
                continue
            if c == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if c in ('{', '['):
                stack.append(c)
            elif c == '}' and stack and stack[-1] == '{':
                stack.pop()
            elif c == ']' and stack and stack[-1] == '[':
                stack.pop()
            elif c == ',':
                last_comma_outside_string = i

        if not stack:
            return None

        # JSON troncato: tronca all'ultima virgola completa (ultimo valore intero)
        # poi chiudi le parentesi/graffe rimaste aperte
        if last_comma_outside_string > 0:
            truncated = candidate[:last_comma_outside_string]
        elif not in_string:
            truncated = candidate.rstrip().rstrip(',')
        else:
            # Stringa aperta senza virgole: tronca tutto dopo il primo {/[
            truncated = candidate[0]

        # Ricalcola stack dopo troncamento
        stack = []
        in_string = False
        escape_next = False
        for c in truncated:
            if escape_next:
                escape_next = False
                continue
            if c == '\\' and in_string:
                escape_next = True
                continue
            if c == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if c in ('{', '['):
                stack.append(c)
            elif c == '}' and stack and stack[-1] == '{':
                stack.pop()
            elif c == ']' and stack and stack[-1] == '[':
                stack.pop()

        # Chiudi parentesi/graffe aperte nell'ordine inverso dello stack
        # es. stack = ['{', '[', '{'] → chiusura = '}', ']', '}'
        closing_map = {'{': '}', '[': ']'}
        closing = ''.join(closing_map[c] for c in reversed(stack))

        repaired = truncated.rstrip().rstrip(',') + closing

        try:
            result = json.loads(repaired)
            logger.warning(
                f"JSON riparato (troncato da max_tokens, chiuse {len(stack)} parentesi: "
                f"{''.join(stack)} → {closing})"
            )
            return result
        except json.JSONDecodeError:
            return None

    def _classify_api_error(self, error: Exception) -> str:
        """Classifica il tipo di errore API per logging strutturato."""
        error_str = str(error).lower()
        if "rate" in error_str or "429" in error_str:
            return "rate_limit"
        if "timeout" in error_str or "timed out" in error_str:
            return "timeout"
        if "too large" in error_str or "token" in error_str or "413" in error_str:
            return "request_too_large"
        if "connection" in error_str or "network" in error_str:
            return "network"
        if "400" in error_str:
            return "bad_request"
        if "500" in error_str or "502" in error_str or "503" in error_str:
            return "server_error"
        return "unknown"

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
5. Usa confidence >= 0.8 se il contenuto corrisponde chiaramente a una categoria
6. DSAN non corrisponde per forza ad un documento di tipo dsan che non sono tra quelli da importare (es. dsan di gara,DSAN fiscale e contrib_,  dsan di pagamento, dsan di liquidazione) ma potrebbe essere un documento generico che contiene la parola "dsan" senza essere un vero documento di tipo dsan. In caso di dubbio, usa UNKNOWN.
7. File "Dichiarazione_*" e documenti di manleva/subappalto NON sono note di trasmissione. Sono dichiarazioni accessorie = UNKNOWN.
8. Il PERCORSO CARTELLA è un indizio importante: se il file è in una cartella "FATTURE SUBAPPALTO" o "AUTORIZZAZIONE SUBAPPALTO" o "manleva", probabilmente NON è una nota di trasmissione.
"""
        return f"""{hints_section}

{categories_section}

{rules_section}"""

    async def classify_document(
        self,
        ocr_content: str,
        filename: str,
        folder_path: str = None
    ) -> dict:
        """
        Classifica un documento in una delle categorie supportate.

        Args:
            ocr_content: Contenuto OCR del documento
            filename: Nome del file (usato come hint)
            folder_path: Percorso completo del file su storage (usato per hint cartella)

        Returns:
            Dict con categoria, confidence e motivo
        """
        await claude_limiter.acquire()

        # Costruisci prompt dinamico dalla configurazione
        dynamic_categories = self._build_classification_prompt()

        # Estrai hint dalla cartella (percorso completo per contesto migliore)
        folder_hint = ""
        if folder_path:
            # Prendi le cartelle dal percorso (escluso il filename)
            parts = folder_path.replace('\\', '/').split('/')
            if len(parts) > 1:
                # Cartella diretta del file
                parent_folder = parts[-2]
                # Mostra percorso completo delle cartelle per massimo contesto
                full_folder_path = '/'.join(parts[:-1])
                folder_hint = f"\nPERCORSO CARTELLA: {full_folder_path}"
                # Controlla se la cartella corrisponde a un tipo documento noto
                doc_types_config = load_document_types_from_db()
                for doc_type_code in doc_types_config:
                    if parent_folder.upper() == doc_type_code.upper():
                        folder_hint += (
                            f"\nATTENZIONE: Il file si trova nella cartella '{parent_folder}' "
                            f"che corrisponde al tipo documento {doc_type_code}. "
                            f"Questo è un FORTE indizio sulla classificazione."
                        )
                        break

        # Tronca OCR per classificazione: prime+ultime pagine bastano per riconoscere il tipo
        # Limite ~150K caratteri (~50K token) per restare sotto i 200K token totali del prompt
        MAX_CLASSIFY_CHARS = 150_000
        if len(ocr_content) > MAX_CLASSIFY_CHARS:
            half = MAX_CLASSIFY_CHARS // 2
            ocr_truncated = (
                ocr_content[:half]
                + f"\n\n[... CONTENUTO TRONCATO: {len(ocr_content)} caratteri totali, "
                f"mostrati primi e ultimi {half} caratteri ...]\n\n"
                + ocr_content[-half:]
            )
            logger.info(
                f"[{filename}] OCR troncato per classificazione: "
                f"{len(ocr_content)} -> {len(ocr_truncated)} caratteri"
            )
        else:
            ocr_truncated = ocr_content

        prompt = f"""Analizza il NOME DEL FILE, la CARTELLA e il contenuto OCR per classificare il documento.

NOME FILE: {filename}{folder_hint}
CONTENUTO OCR:
{ocr_truncated}

{dynamic_categories}

Rispondi SOLO in JSON (senza testo aggiuntivo):
{{"categoria": "CODICE", "confidence": 0.0-1.0, "motivo": "breve spiegazione"}}"""

        try:
            response = await self.client.messages.create(
                model=CLASSIFICATION_MODEL,
                max_tokens=300,
                temperature=0.1,
                messages=[{"role": "user", "content": prompt}]
            )

            result = self._extract_json(response.content[0].text)

            # Se bassa confidence, forza UNKNOWN
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
        extraction_prompt: str,
        model: str = None
    ) -> dict:
        """
        Estrae dati strutturati da un documento.

        Args:
            ocr_content: Contenuto OCR del documento
            filename: Nome del file
            doc_type: Tipo documento
            extraction_prompt: Prompt specifico per l'estrazione

        Returns:
            Dict con dati estratti
        """
        await claude_limiter.acquire()

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
            async with self.client.messages.stream(
                model=model or self.model,
                max_tokens=64000,
                temperature=0.1,
                messages=[{"role": "user", "content": full_prompt}]
            ) as stream:
                response = await stream.get_final_message()

            result = self._extract_json(response.content[0].text)
            logger.info(f"Dati estratti per {doc_type}: {len(result)} campi")
            return result

        except Exception as e:
            error_type = self._classify_api_error(e)
            logger.error(f"Errore estrazione dati [{error_type}]: {e}")
            return {}

    async def extract_data_from_images(
        self,
        page_images: list[str],
        filename: str,
        doc_type: str,
        extraction_prompt: str,
        model: str = None
    ) -> dict:
        """
        Estrae dati strutturati inviando tutte le immagini in una singola chiamata (batch).

        Args:
            page_images: Lista di immagini base64 delle pagine
            filename: Nome del file
            doc_type: Tipo documento
            extraction_prompt: Prompt specifico per l'estrazione

        Returns:
            Dict con dati estratti
        """
        await claude_limiter.acquire()

        # Limite API: max 20 immagini per chiamata
        if len(page_images) > 20:
            logger.warning(
                f"[{filename}] {len(page_images)} pagine superano il limite di 20 immagini. "
                f"Invio prime 5 + ultime 15 per catturare header e riepilogo."
            )
            # Strategia: prime 5 pagine (header/dati principali) + ultime 15 (riepiloghi/totali)
            if len(page_images) > 25:
                page_images = page_images[:5] + page_images[-15:]
            else:
                page_images = page_images[:20]

        logger.info(
            f"[{filename}] Extracting via vision (batch): {len(page_images)} images"
        )

        # Costruisci contenuto messaggio con N blocchi immagine + prompt
        content = []
        for idx, img_base64 in enumerate(page_images):
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": img_base64
                }
            })

        content.append({
            "type": "text",
            "text": f"""Analizza le immagini di questo documento di tipo {doc_type}.

NOME FILE: {filename}

{extraction_prompt}

IMPORTANTE:
- Se un dato non è presente o non chiaro, usa null
- I numeri devono essere senza separatore delle migliaia (usa . come decimale)
- Le date devono essere in formato YYYY-MM-DD
- Estrai codici (CIG, CUP, Protocollo) esattamente come appaiono
- Se il nome file contiene dati non trovati nelle immagini, usa quelli del nome file

Rispondi SOLO con il JSON richiesto, senza spiegazioni."""
        })

        try:
            async with self.client.messages.stream(
                model=model or self.model,
                max_tokens=64000,
                temperature=0.1,
                messages=[{"role": "user", "content": content}]
            ) as stream:
                response = await stream.get_final_message()

            result = self._extract_json(response.content[0].text)
            logger.info(f"Dati estratti via vision (batch) per {doc_type}: {len(result)} campi")
            return result

        except Exception as e:
            error_type = self._classify_api_error(e)
            logger.error(
                f"Errore estrazione vision batch [{error_type}]: {e}"
            )
            return {}

    async def extract_data_from_single_image(
        self,
        page_image: str,
        filename: str,
        doc_type: str,
        extraction_prompt: str,
        page_num: int,
        total_pages: int,
        model: str = None
    ) -> dict:
        """
        Estrae dati da una singola immagine di pagina.
        Usato in modalita' per-page (vision_batch=0).

        Args:
            page_image: Immagine base64 della pagina
            filename: Nome del file
            doc_type: Tipo documento
            extraction_prompt: Prompt specifico per l'estrazione
            page_num: Numero pagina corrente (1-indexed)
            total_pages: Totale pagine

        Returns:
            Dict con dati estratti dalla pagina
        """
        await claude_limiter.acquire()

        logger.info(
            f"[{filename}] Extracting via vision (per_page): page {page_num}/{total_pages}"
        )

        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": page_image
                }
            },
            {
                "type": "text",
                "text": f"""Analizza questa pagina ({page_num}/{total_pages}) del documento di tipo {doc_type}.

NOME FILE: {filename}

{extraction_prompt}

IMPORTANTE:
- Se un dato non è presente in QUESTA pagina, usa null
- I numeri devono essere senza separatore delle migliaia (usa . come decimale)
- Le date devono essere in formato YYYY-MM-DD
- Estrai SOLO i dati visibili in questa pagina

Rispondi SOLO con il JSON richiesto, senza spiegazioni."""
            }
        ]

        try:
            async with self.client.messages.stream(
                model=model or self.model,
                max_tokens=64000,
                temperature=0.1,
                messages=[{"role": "user", "content": content}]
            ) as stream:
                response = await stream.get_final_message()

            result = self._extract_json(response.content[0].text)
            logger.debug(
                f"[{filename}] Pagina {page_num}: {len(result)} campi estratti via vision"
            )
            return result

        except Exception as e:
            logger.error(f"Errore estrazione vision pagina {page_num}: {e}")
            return {}

    async def fix_sql_query(
        self,
        original_query: str,
        error_message: str,
        json_data: dict,
        doc_type: str
    ) -> Optional[str]:
        """
        Usa Claude per correggere una query SQL fallita.

        Args:
            original_query: Query che ha causato errore
            error_message: Messaggio di errore MySQL
            json_data: Dati estratti originali
            doc_type: Tipo documento

        Returns:
            Query corretta o None se non riesce
        """
        await claude_limiter.acquire()

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
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                temperature=0.1,
                messages=[{"role": "user", "content": prompt}]
            )

            corrected_query = response.content[0].text.strip()

            # Rimuovi eventuale markdown
            if corrected_query.startswith("```"):
                corrected_query = corrected_query.split("```")[1]
                if corrected_query.startswith("sql"):
                    corrected_query = corrected_query[3:]

            return corrected_query.strip() if corrected_query else None

        except Exception as e:
            logger.error(f"Errore correzione query: {e}")
            return None
