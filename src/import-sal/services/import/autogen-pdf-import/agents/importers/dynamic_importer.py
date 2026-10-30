"""
Dynamic Importer
Importer generico che usa la configurazione dinamica dal database.
Il prompt di estrazione viene generato automaticamente dai campi della tabella.
"""
import asyncio
import re
import logging
from typing import Optional, Any

from config.document_types import get_document_config, load_document_types_from_db
from services.claude_service import ClaudeService
from services.db_service import DBService
from utils.sql_utils import safe_value, safe_number, safe_date, safe_enum

logger = logging.getLogger(__name__)


class DynamicImporter:
    """
    Importer dinamico che gestisce qualsiasi tipo documento.

    La configurazione viene dal database (tabella autogen_document_types).
    I campi vengono scoperti automaticamente via DESCRIBE della tabella destinazione.
    Il prompt di estrazione e la query SQL vengono generati automaticamente.

    Per aggiungere un nuovo tipo documento:
    1. Creare la tabella destinazione con commenti sulle colonne
    2. Inserire record in autogen_document_types
    3. NESSUNA modifica al codice Python necessaria!
    """

    def __init__(self, doc_type: str):
        """
        Inizializza importer per un tipo documento specifico

        Args:
            doc_type: Codice tipo documento (es. "AQ", "FATTURA")
        """
        self.doc_type = doc_type
        self.config = get_document_config(doc_type)

        if not self.config:
            raise ValueError(f"Tipo documento non configurato: {doc_type}")

        self.claude_service = ClaudeService()
        self.db_service = DBService()

        logger.info(f"Inizializzato DynamicImporter per {doc_type}: {self.config['name']}")

    @property
    def tables(self) -> dict:
        return self.config.get("tables", {})

    @property
    def fields(self) -> dict:
        return self.config.get("fields", {})

    @property
    def detail_fields(self) -> dict:
        return self.config.get("detail_fields")

    @property
    def has_detail_table(self) -> bool:
        """True se c'è una tabella dettaglio configurata"""
        return bool(self.config.get("tables", {}).get("detail"))

    @property
    def vision_batch(self) -> bool:
        """True = tutte le immagini in 1 chiamata, False = 1 chiamata per pagina"""
        return self.config.get("vision_batch", True)

    @property
    def extraction_model(self) -> str:
        """Modello Claude da usare per estrazione dati (configurabile per doc type)"""
        return self.config.get("extraction_model", "claude-sonnet-4-5")

    def get_extraction_prompt(self) -> str:
        """
        Genera dinamicamente il prompt per l'estrazione dati
        basandosi sui campi scoperti dalla tabella.

        Se extraction_notes sono presenti, vengono messe IN PRIMO PIANO
        perché contengono istruzioni specifiche sul formato di estrazione
        (es. multi-record con array "righe") che hanno priorità sulla
        lista campi auto-generata.
        """
        import json

        extraction_notes = self.config.get("extraction_notes", "")
        has_custom_format = bool(extraction_notes and "righe" in extraction_notes)

        # Costruisci descrizione campi dalla tabella DB
        fields_desc = {}
        for field_name, field_config in self.fields.items():
            description = field_config.get("description", field_name.replace('_', ' ').title())
            field_type = field_config.get("type", "string")

            if field_type == "date":
                description += " (formato YYYY-MM-DD)"
            elif field_type == "number":
                description += " (numero decimale, senza simbolo valuta)"
            elif field_type == "integer":
                description += " (numero intero)"
            elif field_type == "boolean":
                description += " (true/false)"

            if field_config.get("required"):
                description += " [OBBLIGATORIO]"

            fields_desc[field_name] = description

        fields_formatted = json.dumps(fields_desc, indent=4, ensure_ascii=False)

        # Costruisci descrizione campi dettaglio (escludendo FK auto-gestite dal codice)
        detail_formatted = None
        if self.detail_fields:
            auto_fk_columns = {'id_fattura', 'vap_numero', 'parent_id'}
            detail_fk_col = self.config.get('detail_fk_column')
            if detail_fk_col:
                auto_fk_columns.add(detail_fk_col)
            detail_desc = {}
            for field_name, field_config in self.detail_fields.items():
                if field_name in auto_fk_columns:
                    continue
                description = field_config.get("description", field_name.replace('_', ' ').title())
                field_type = field_config.get("type", "string")

                if field_type == "date":
                    description += " (formato YYYY-MM-DD)"
                elif field_type == "number":
                    description += " (numero decimale)"
                elif field_type == "integer":
                    description += " (numero intero)"

                detail_desc[field_name] = description

            if detail_desc:
                detail_formatted = json.dumps(detail_desc, indent=4, ensure_ascii=False)

        # Se ci sono extraction_notes con formato "righe", struttura il prompt
        # dando PRIORITÀ alle notes per evitare conflitto con la lista campi piatta
        if has_custom_format:
            prompt = f"""Estrai i dati dal documento di tipo "{self.config['name']}".

ISTRUZIONI DI ESTRAZIONE (SEGUI QUESTE CON PRIORITÀ ASSOLUTA):
{extraction_notes}

CAMPI HEADER (livello top del JSON):
{fields_formatted}
"""
            if detail_formatted:
                prompt += f"""
CAMPI PER OGNI RIGA DI DETTAGLIO (array "righe"):
{detail_formatted}
"""
            prompt += """
REGOLE GENERALI:
- Se un dato non è presente o non chiaro, usa null
- I numeri devono usare il PUNTO come separatore decimale (es. 55.794, NON 55,794)
- Le date devono essere in formato YYYY-MM-DD
- Estrai SOLO valori presenti nel documento, NON inventare o stimare valori
- Rispondi SOLO con il JSON, senza spiegazioni."""
        else:
            # Prompt standard per documenti senza formato custom
            prompt = f"""Estrai i seguenti dati dal documento di tipo "{self.config['name']}":

{fields_formatted}

"""
            # Aggiungi istruzioni per righe dettaglio se presenti
            if detail_formatted:
                prompt += f"""
OBBLIGATORIO: Estrai le righe di dettaglio del documento nell'array "righe".
Campi per ogni riga di dettaglio:
{detail_formatted}

L'array "righe" è obbligatorio anche se c'è una sola riga nel documento.
"""

            # Aggiungi note specifiche se presenti (senza formato righe)
            if extraction_notes:
                prompt += f"\nNOTA IMPORTANTE: {extraction_notes}"

            prompt += """

ISTRUZIONI:
- Se un dato non è presente o non chiaro, usa null
- I numeri devono essere senza separatore delle migliaia (usa . come decimale)
- Le date devono essere in formato YYYY-MM-DD
- Estrai codici (CIG, CUP, Protocollo, etc.) esattamente come appaiono
- Se il nome file contiene dati non trovati nell'OCR, potranno essere integrati dopo

Rispondi SOLO con il JSON, senza spiegazioni."""

        return prompt

    def extract_from_filename(self, filename: str) -> dict:
        """
        Estrae dati dal nome file usando i pattern configurati
        """
        data = {}
        patterns = self.config.get("filename_patterns", [])

        for pattern in patterns:
            try:
                match = re.search(pattern, filename, re.IGNORECASE)
                if match:
                    # Estrai tutti i gruppi catturati
                    groups = match.groups()
                    if groups:
                        # Il primo gruppo viene usato come valore principale
                        # Cerca di capire quale campo è basandosi sul pattern
                        value = groups[0]
                        if value:
                            value = value.replace('_', ' ').replace('.pdf', '').replace('.p7m', '').strip()
                            # Salva come _filename_extracted per merge successivo
                            data['_filename_value'] = value
                    break
            except re.error as e:
                logger.warning(f"Pattern regex invalido '{pattern}': {e}")

        return data

    async def extract(
        self,
        ocr_content: str,
        filename: str,
        pdf_url: str,
        page_images: list[str] = None
    ) -> dict:
        """
        Estrae dati strutturati dal contenuto OCR o dalle immagini delle pagine.

        Se page_images sono disponibili, usa sempre vision extraction
        (batch o per-page in base a vision_batch config).
        Fallback a estrazione da testo OCR se no immagini.
        """
        # Estrai dati dal nome file come fallback
        filename_data = self.extract_from_filename(filename)

        # Costruisci prompt
        prompt = self.get_extraction_prompt()

        # Estrazione: vision (preferita) o testo OCR (fallback)
        extracted = {}
        if page_images:
            if self.vision_batch:
                # Batch: tutte le immagini in 1 chiamata
                extracted = await self.claude_service.extract_data_from_images(
                    page_images=page_images,
                    filename=filename,
                    doc_type=self.doc_type,
                    extraction_prompt=prompt,
                    model=self.extraction_model
                )
            else:
                # Per-page: tutte le pagine in parallelo, poi merge
                total = len(page_images)
                tasks = [
                    self.claude_service.extract_data_from_single_image(
                        page_image=img,
                        filename=filename,
                        doc_type=self.doc_type,
                        extraction_prompt=prompt,
                        page_num=idx,
                        total_pages=total,
                        model=self.extraction_model
                    )
                    for idx, img in enumerate(page_images, 1)
                ]
                results = await asyncio.gather(*tasks)
                per_page_results = [r for r in results if r]
                extracted = self._merge_per_page_results(per_page_results)

            # Fallback: se vision ha fallito (dict vuoto), riprova con testo OCR
            if not extracted and ocr_content:
                logger.warning(
                    f"[{self.doc_type}] Vision extraction fallita per {filename} "
                    f"({len(page_images)} pagine), fallback a testo OCR"
                )
                extracted = await self.claude_service.extract_data(
                    ocr_content=ocr_content,
                    filename=filename,
                    doc_type=self.doc_type,
                    extraction_prompt=prompt,
                    model=self.extraction_model
                )

        if not extracted:
            # Nessuna immagine disponibile o tutti i tentativi falliti
            extracted = await self.claude_service.extract_data(
                ocr_content=ocr_content,
                filename=filename,
                doc_type=self.doc_type,
                extraction_prompt=prompt,
                model=self.extraction_model
            )

        # Normalizza nomi campi estratti per matchare colonne DB
        # (es. "contributo_competenza" -> "contributo di competenza")
        extracted = self._normalize_field_names(extracted)

        # Merge con dati da filename per valori mascherati o mancanti
        merged = self._merge_with_filename_data(extracted, filename_data)

        # Risolvi cod_dipendente per documenti dipendenti
        merged = self._resolve_all_fks(merged)

        # Log campi mancanti
        missing = self._get_missing_required_fields(merged)
        if missing:
            logger.warning(f"Campi richiesti mancanti per {self.doc_type}: {missing}")

        return merged

    def _normalize_field_names(self, data: dict) -> dict:
        """
        Normalizza i nomi dei campi estratti da Claude per matchare le colonne DB.
        Gestisce mismatch underscore/spazi (es. contributo_competenza -> contributo di competenza).
        Applica ricorsivamente ai record dentro "righe".
        """
        if not isinstance(data, dict):
            return data

        # Costruisci mappa: nome_normalizzato -> nome_db_reale
        # Combina fields principali e detail_fields
        all_db_fields = set(self.fields.keys())
        if self.detail_fields:
            all_db_fields.update(self.detail_fields.keys())

        # Mappa: chiave con underscore -> nome DB reale (con spazi)
        normalize_map = {}
        for db_name in all_db_fields:
            underscore_variant = db_name.replace(' ', '_')
            if underscore_variant != db_name:
                normalize_map[underscore_variant] = db_name

        if not normalize_map:
            return data

        # Rinomina campi top-level
        normalized = {}
        renamed = []
        for key, value in data.items():
            if key in normalize_map and key not in all_db_fields:
                new_key = normalize_map[key]
                normalized[new_key] = value
                renamed.append(f"'{key}' -> '{new_key}'")
            else:
                normalized[key] = value

        # Rinomina campi dentro "righe"
        if "righe" in normalized and isinstance(normalized["righe"], list):
            new_righe = []
            for row in normalized["righe"]:
                if isinstance(row, dict):
                    new_row = {}
                    for key, value in row.items():
                        if key in normalize_map and key not in all_db_fields:
                            new_row[normalize_map[key]] = value
                            if f"'{key}' -> '{normalize_map[key]}'" not in renamed:
                                renamed.append(f"'{key}' -> '{normalize_map[key]}'")
                        else:
                            new_row[key] = value
                    new_righe.append(new_row)
                else:
                    new_righe.append(row)
            normalized["righe"] = new_righe

        if renamed:
            logger.info(f"[{self.doc_type}] Campi rinominati: {', '.join(renamed)}")

        return normalized

    def _merge_with_filename_data(self, extracted, filename_data: dict) -> dict:
        """
        Unisce dati estratti con dati dal nome file.
        I dati filename hanno priorità per valori mascherati (es. 449000xxxx)
        """
        # Safety: se extracted non è un dict (es. Claude ha restituito una lista),
        # prova a recuperare
        if isinstance(extracted, list):
            logger.warning(f"extracted è una lista ({len(extracted)} elementi), normalizzazione")
            if len(extracted) == 1 and isinstance(extracted[0], dict):
                extracted = extracted[0]
            elif all(isinstance(r, dict) for r in extracted):
                tmp = {}
                for r in extracted:
                    for k, v in r.items():
                        if k not in tmp or (v is not None and tmp[k] is None):
                            tmp[k] = v
                extracted = tmp
            else:
                extracted = {}
        elif not isinstance(extracted, dict):
            logger.error(f"extracted ha tipo inatteso: {type(extracted)}")
            extracted = {}

        merged = {**extracted}

        # Se c'è un valore estratto dal filename, prova a usarlo per campi mascherati
        filename_value = filename_data.get('_filename_value')
        if filename_value:
            for key, value in merged.items():
                if value and isinstance(value, str):
                    # Se il valore estratto contiene 'x' (mascherato), usa quello del filename
                    if 'xxxx' in value.lower() or value.lower() == 'null':
                        merged[key] = filename_value
                        logger.debug(f"Campo '{key}' sostituito con valore da filename: {filename_value}")

        return merged

    def _resolve_dipendente_code(self, data: dict) -> dict:
        """
        Per qualsiasi documento che ha cod_dipendente nei campi,
        risolve il codice dal nome del dipendente.
        Cerca il nome in: nome_cognome, nominativo, nome+cognome.

        NON si fida del valore estratto da Claude perché spesso è errato
        (es. codice fiscale o altro testo confuso come codice).
        Risolve SEMPRE dal nome tramite lookup DB.
        """
        # Verifica se questo tipo documento ha cod_dipendente nei campi
        if 'cod_dipendente' not in self.fields:
            return data

        # Cerca il nome dipendente in vari campi possibili
        nome_dipendente = (
            data.get('nome_cognome') or
            data.get('nominativo') or
            self._combine_nome_cognome(data)
        )

        if not nome_dipendente:
            logger.debug(f"Nessun nome dipendente trovato per {self.doc_type}")
            return data

        # Cerca dipendente nel database (SEMPRE, anche se cod_dipendente è già valorizzato)
        dipendente = self.db_service.find_dipendente_by_name(nome_dipendente)

        if dipendente:
            old_val = data.get('cod_dipendente')
            data['cod_dipendente'] = dipendente['cod_dipendente']
            if old_val and old_val != dipendente['cod_dipendente']:
                logger.info(
                    f"Risolto cod_dipendente: {nome_dipendente} -> {dipendente['cod_dipendente']} "
                    f"(sovrascritto valore estratto: {old_val})"
                )
            else:
                logger.info(f"Risolto cod_dipendente: {nome_dipendente} -> {dipendente['cod_dipendente']}")
        else:
            logger.warning(f"cod_dipendente non trovato per: {nome_dipendente}")
            data['cod_dipendente'] = None

        return data

    def _combine_nome_cognome(self, data: dict) -> str | None:
        """Combina campi nome e cognome separati"""
        nome = data.get('nome', '')
        cognome = data.get('cognome', '')
        if nome or cognome:
            return f"{cognome} {nome}".strip()
        return None

    def _resolve_classe_cespiti_fk(self, data: dict) -> dict:
        """
        Per documenti che hanno classe_cespiti_fk nei campi,
        risolve la FK dalla descrizione classe_cespiti.
        """
        # Verifica se questo tipo documento ha classe_cespiti_fk nei campi
        if 'classe_cespiti_fk' not in self.fields:
            return data

        # Se classe_cespiti_fk già valorizzato, skip
        if data.get('classe_cespiti_fk'):
            return data

        # Cerca la descrizione classe cespiti
        classe_desc = data.get('classe_cespiti')
        if not classe_desc:
            logger.debug(f"Nessuna classe_cespiti trovata per {self.doc_type}")
            return data

        # Cerca nel database
        classe = self.db_service.find_classe_cespiti_by_name(classe_desc)

        if classe:
            data['classe_cespiti_fk'] = classe['id_classe_cespiti']
            logger.info(f"Risolto classe_cespiti_fk: {classe_desc} -> {classe['id_classe_cespiti']}")
        else:
            logger.warning(f"classe_cespiti_fk non trovata per: {classe_desc}")

        return data

    def _resolve_qualifica_fk(self, data: dict) -> dict:
        """
        Per documenti che hanno campi cod_qualifica (es. cod_qualifica_nuovo,
        cod_qualifica_precedente), risolve il codice dalla descrizione testuale
        del livello (es. livello_nuovo, livello_precedente).
        """
        # Mappa: campo cod_qualifica -> campo descrizione livello da cui risolvere
        qualifica_mappings = {
            'cod_qualifica_nuovo': 'livello_nuovo',
            'cod_qualifica_precedente': 'livello_precedente',
        }

        for cod_field, desc_field in qualifica_mappings.items():
            # Verifica se il campo cod_qualifica esiste nei fields
            if cod_field not in self.fields:
                continue

            # Cerca la descrizione livello nei dati estratti
            descrizione = data.get(desc_field)
            if not descrizione:
                logger.debug(f"Nessun {desc_field} trovato per risolvere {cod_field}")
                continue

            # Cerca nel database
            qualifica = self.db_service.find_qualifica_by_description(descrizione)

            if qualifica:
                data[cod_field] = qualifica['cod_qualifica']
                logger.info(
                    f"Risolto {cod_field}: {descrizione} -> {qualifica['cod_qualifica']}"
                )
            else:
                logger.warning(f"{cod_field} non risolto per: {descrizione}")

        return data

    def _resolve_categoria_dipendente(self, data: dict) -> dict:
        """
        Per documenti con categoria_dipendente (es. TIMESHEET),
        valida il valore estratto contro la tabella qualifica.
        Se il valore non è valido (es. "A" invece di "A1"),
        prova a risolverlo via fuzzy match sulla tabella qualifica.
        """
        if 'categoria_dipendente' not in self.fields:
            return data

        categoria = data.get('categoria_dipendente')
        if not categoria:
            return data

        categoria = str(categoria).strip().upper()

        # Lookup nella tabella qualifica per validare/correggere
        qualifica = self.db_service.find_qualifica_by_description(categoria)
        if qualifica:
            # Usa la des_qualifica corretta dalla tabella
            data['categoria_dipendente'] = qualifica['des_qualifica']
            if categoria != qualifica['des_qualifica']:
                logger.info(
                    f"categoria_dipendente corretta: '{categoria}' -> '{qualifica['des_qualifica']}'"
                )
        else:
            logger.warning(
                f"categoria_dipendente '{categoria}' non trovata in tabella qualifica, "
                f"mantengo valore estratto"
            )

        return data

    def _resolve_all_fks(self, data: dict) -> dict:
        """Risolve tutte le FK supportate"""
        data = self._resolve_dipendente_code(data)
        data = self._resolve_classe_cespiti_fk(data)
        data = self._resolve_qualifica_fk(data)
        data = self._resolve_categoria_dipendente(data)
        return data

    def _merge_per_page_results(self, results: list[dict]) -> dict:
        """
        Merge risultati JSON da estrazione per-page.
        Campi comuni: presi dalla prima occorrenza non-null.
        Array "righe": concatenati da tutte le pagine.
        """
        if not results:
            return {}

        if len(results) == 1:
            return results[0]

        merged = {}
        all_righe = []

        for page_result in results:
            # Raccogli righe da ogni pagina
            if "righe" in page_result:
                righe = page_result["righe"]
                if isinstance(righe, list):
                    all_righe.extend(righe)

            # Merge campi comuni (prima occorrenza non-null vince)
            for key, value in page_result.items():
                if key == "righe":
                    continue
                if key not in merged or merged[key] is None:
                    merged[key] = value

        # Aggiungi righe concatenate
        if all_righe:
            merged["righe"] = all_righe

        logger.info(
            f"Merge per-page: {len(results)} pagine -> "
            f"{len(merged)} campi, {len(all_righe)} righe"
        )
        return merged

    def _get_missing_required_fields(self, data: dict) -> list[str]:
        """Ritorna lista campi richiesti mancanti"""
        missing = []
        for field_name, field_config in self.fields.items():
            if field_config.get("required") and not data.get(field_name):
                missing.append(field_name)
        return missing

    def _validate_required_fields(self, data: dict) -> tuple[bool, list[str]]:
        """
        Verifica che tutti i campi NOT NULL (senza default) abbiano un valore.

        Returns:
            (is_valid, missing_fields) - is_valid=True se tutti i campi obbligatori sono presenti
        """
        missing = self._get_missing_required_fields(data)
        if missing:
            logger.warning(
                f"[{self.doc_type}] Campi obbligatori mancanti: {missing}"
            )
            return False, missing
        return True, []

    def generate_sql(self, data: dict, sal_number: int, pdf_url: str) -> str:
        """
        Genera query SQL usando il template configurato.
        Sostituisce i placeholder con i valori effettivi.
        """
        sql_template = self.config.get("sql_template", "")

        if not sql_template:
            raise ValueError(f"SQL template non configurato per {self.doc_type}")

        # Prepara i valori per la sostituzione
        values = self._prepare_sql_values(data, sal_number, pdf_url)

        # Sostituisci i placeholder nel template
        try:
            sql = sql_template.format(**values)
        except KeyError as e:
            logger.error(f"Campo mancante nel template SQL: {e}")
            # Prova a gestire i campi mancanti con NULL
            sql = self._safe_format_sql(sql_template, values)

        return sql

    def _safe_format_sql(self, template: str, values: dict) -> str:
        """
        Formatta SQL template gestendo campi mancanti con NULL
        """
        import re

        def replacer(match):
            key = match.group(1)
            return values.get(key, 'NULL')

        return re.sub(r'\{(\w+)\}', replacer, template)

    def _prepare_sql_values(self, data: dict, sal_number: int, pdf_url: str) -> dict:
        """
        Prepara i valori per la sostituzione nel template SQL
        """
        values = {
            "sal_number": sal_number,
            "pdf_path": safe_value(pdf_url, 500),
            "pdf_filename": safe_value(pdf_url.split('/')[-1] if pdf_url else None, 255)
        }

        # Processa ogni campo configurato
        for field_name, field_config in self.fields.items():
            raw_value = data.get(field_name)
            field_type = field_config.get("type", "string")
            max_length = field_config.get("max_length")

            # Converti in base al tipo
            if raw_value is None:
                values[field_name] = "NULL"
            elif field_type == "enum":
                # Gestione ENUM safe
                allowed_values = field_config.get("allowed_values", [])
                default_value = field_config.get("default")
                values[field_name] = safe_enum(raw_value, allowed_values, default_value)
            elif field_type == "string":
                values[field_name] = safe_value(raw_value, max_length)
            elif field_type == "number":
                values[field_name] = safe_number(raw_value)
            elif field_type == "integer":
                try:
                    values[field_name] = str(int(float(str(raw_value).replace(',', '.'))))
                except (ValueError, TypeError):
                    values[field_name] = "NULL"
            elif field_type == "date":
                values[field_name] = safe_date(raw_value)
            elif field_type == "boolean":
                if isinstance(raw_value, bool):
                    values[field_name] = "1" if raw_value else "0"
                elif str(raw_value).lower() in ('true', '1', 'yes', 'si', 'sì'):
                    values[field_name] = "1"
                elif str(raw_value).lower() in ('false', '0', 'no'):
                    values[field_name] = "0"
                else:
                    values[field_name] = "NULL"
            else:
                values[field_name] = safe_value(raw_value, max_length)

        return values

    async def import_to_db(
        self,
        data: dict,
        sal_number: int,
        pdf_url: str
    ) -> tuple[bool, Optional[str]]:
        """
        Importa i dati nel database.
        Supporta documenti multi-record (es. TIMESHEET con più mesi).
        """
        try:
            # Normalizza: se Claude ha usato un nome diverso per l'array di righe
            # (es. "linee_intervento" invece di "righe"), rinomina a "righe".
            # Solo per tipi multi-record (extraction_notes con "righe" e senza detail_table)
            # per evitare di convertire array non correlati in doc single-record (es. BDC)
            extraction_notes = self.config.get("extraction_notes", "")
            is_multi_record_type = "righe" in extraction_notes and not self.has_detail_table
            if not data.get("righe") and is_multi_record_type:
                for key, value in list(data.items()):
                    if isinstance(value, list) and value and isinstance(value[0], dict):
                        logger.info(f"[{self.doc_type}] Rinominato array '{key}' -> 'righe' ({len(value)} elementi)")
                        data["righe"] = data.pop(key)
                        break

            # Modalità multi-record: "righe" presente MA nessuna detail_table
            # → ogni riga diventa un record principale (es. TIMESHEET mesi)
            if data.get("righe") and not self.has_detail_table:
                return await self._import_multi_record(data, sal_number, pdf_url)

            # Validazione campi obbligatori prima di INSERT
            is_valid, missing = self._validate_required_fields(data)
            if not is_valid:
                return False, f"Campi obbligatori mancanti: {', '.join(missing)}"

            # Modalità standard: un PDF = un record principale
            query = self.generate_sql(data, sal_number, pdf_url)

            success, error, parent_id = await self.db_service.execute_insert_with_retry(
                query=query,
                json_data=data,
                doc_type=self.doc_type,
                max_retries=2
            )

            if not success:
                return False, error

            # Se ci sono righe di dettaglio, inseriscile
            if self.config.get("detail_sql_template") and data.get("righe"):
                # Se la tabella principale non ha auto-increment (es. bdc_pdf con PK = id_ordine),
                # lastrowid torna 0. In quel caso recupera il valore FK dai dati estrattti.
                if not parent_id:
                    detail_fk_col = self.config.get('detail_fk_column')
                    if detail_fk_col and detail_fk_col in data:
                        try:
                            parent_id = int(float(str(data[detail_fk_col])))
                            logger.info(f"parent_id recuperato dai dati: {detail_fk_col}={parent_id}")
                        except (ValueError, TypeError):
                            logger.error(f"Impossibile recuperare parent_id da {detail_fk_col}={data.get(detail_fk_col)}")

                detail_success, detail_error = await self._import_detail_rows(
                    data.get("righe", []),
                    data,
                    sal_number,
                    parent_id
                )
                if not detail_success:
                    logger.warning(f"Errore inserimento righe dettaglio: {detail_error}")

            return True, None

        except Exception as e:
            logger.exception(f"Errore import {self.doc_type}")
            return False, str(e)

    async def _import_multi_record(
        self,
        data: dict,
        sal_number: int,
        pdf_url: str
    ) -> tuple[bool, Optional[str]]:
        """
        Importa documenti multi-record (es. TIMESHEET con più mesi, IVA_INDETRAIBILE).
        Ogni elemento dell'array "righe" diventa un record nella tabella principale.

        Fase 1: Preparazione (CPU, no DB) - merge campi, FK resolution, genera SQL
        Fase 2: Esecuzione batch - tutte le query in 1 transazione
        Fallback: se batch fallisce, degrada a inserimento singolo
        """
        records = data.get("righe", [])
        if not records:
            logger.warning(f"Nessun record in 'righe'")
            return False, "Array 'righe' vuoto"

        # Campi comuni da copiare in ogni record (escluso l'array righe)
        common_fields = {k: v for k, v in data.items() if k != "righe"}

        # === FASE 1: Preparazione query ===
        queries = []
        prep_errors = []

        for idx, record in enumerate(records):
            try:
                # Merge campi comuni con campi specifici del record
                merged_record = {**common_fields, **record}

                # Risolvi FK
                merged_record = self._resolve_all_fks(merged_record)

                # Validazione per-record: skip con warning (non blocca altri record)
                is_valid, missing = self._validate_required_fields(merged_record)
                if not is_valid:
                    prep_errors.append(
                        f"Record {idx + 1}: campi obbligatori mancanti: {', '.join(missing)}"
                    )
                    logger.warning(
                        f"Multi-record {idx + 1}/{len(records)} skippato: "
                        f"mancano {missing}"
                    )
                    continue

                # Genera SQL
                query = self.generate_sql(merged_record, sal_number, pdf_url)
                queries.append(query)

            except Exception as e:
                prep_errors.append(f"Record {idx + 1}: {str(e)}")

        if not queries:
            return False, "; ".join(prep_errors) if prep_errors else "Nessuna query generata"

        # === FASE 2: Esecuzione batch ===
        success_count, batch_errors = await self.db_service.execute_batch_insert(queries)

        all_errors = prep_errors + batch_errors

        if success_count == 0 and len(queries) > 1:
            # Batch fallito completamente: fallback a inserimento singolo
            logger.warning(
                f"Batch fallito ({', '.join(batch_errors[:2])}), "
                f"fallback a inserimento singolo per {len(queries)} query"
            )
            success_count = 0
            all_errors = list(prep_errors)  # reset batch errors

            for idx, query in enumerate(queries):
                try:
                    success, error, _ = await self.db_service.execute_insert_with_retry(
                        query=query,
                        json_data=data,
                        doc_type=self.doc_type,
                        max_retries=2
                    )
                    if success:
                        success_count += 1
                    else:
                        all_errors.append(f"Fallback query {idx + 1}: {error}")
                except Exception as e:
                    all_errors.append(f"Fallback query {idx + 1}: {str(e)}")

        logger.info(f"Multi-record completato: {success_count}/{len(records)} inseriti")

        if all_errors:
            return success_count > 0, "; ".join(all_errors)

        return True, None

    async def _import_detail_rows(
        self,
        rows: list[dict],
        main_data: dict,
        sal_number: int,
        parent_id: int | None = None
    ) -> tuple[bool, Optional[str]]:
        """
        Importa le righe di dettaglio in batch (1 transazione).

        Fase 1: Preparazione (CPU, no DB) - genera SQL per ogni riga
        Fase 2: Esecuzione batch - tutte le query in 1 transazione
        Fallback: se batch fallisce, degrada a inserimento singolo

        Args:
            rows: Lista di righe da inserire
            main_data: Dati del record principale
            sal_number: Numero SAL
            parent_id: ID del record padre (per FK)
        """
        detail_template = self.config.get("detail_sql_template", "")

        if not detail_template or not rows:
            return True, None

        # === FASE 1: Preparazione query ===
        queries = []
        prep_errors = []

        for idx, row in enumerate(rows):
            try:
                # Prepara valori per la riga
                values = {}

                # Prima processa i campi estratti dal documento
                if self.detail_fields:
                    for field_name, field_config in self.detail_fields.items():
                        raw_value = row.get(field_name)
                        field_type = field_config.get("type", "string")
                        max_length = field_config.get("max_length")

                        if raw_value is None:
                            values[field_name] = "NULL"
                        elif field_type == "enum":
                            allowed_values = field_config.get("allowed_values", [])
                            default_value = field_config.get("default")
                            values[field_name] = safe_enum(raw_value, allowed_values, default_value)
                        elif field_type == "number":
                            values[field_name] = safe_number(raw_value)
                        elif field_type == "integer":
                            try:
                                values[field_name] = str(int(float(str(raw_value))))
                            except:
                                values[field_name] = "NULL"
                        else:
                            values[field_name] = safe_value(raw_value, max_length)

                # POI aggiungi parent_id per la foreign key (DOPO detail_fields per evitare sovrascritture)
                if parent_id is not None:
                    detail_fk_col = self.config.get('detail_fk_column')
                    if detail_fk_col:
                        # FK rilevata dinamicamente da information_schema
                        values[detail_fk_col] = str(parent_id)
                        logger.debug(f"Riga {idx}: {detail_fk_col}={parent_id} (FK dinamica)")
                    else:
                        # Fallback legacy per doc type senza FK rilevata
                        values['parent_id'] = str(parent_id)
                        values['vap_numero'] = str(parent_id)
                        values['id_fattura'] = str(parent_id)
                        logger.debug(f"Riga {idx}: parent_id={parent_id} (fallback legacy)")

                query = self._safe_format_sql(detail_template, values)
                queries.append(query)

            except Exception as e:
                prep_errors.append(f"Riga {idx}: {str(e)}")

        if not queries:
            return False, "; ".join(prep_errors) if prep_errors else "Nessuna query generata"

        # === FASE 2: Esecuzione batch ===
        success_count, batch_errors = await self.db_service.execute_batch_insert(queries)

        all_errors = prep_errors + batch_errors

        if success_count == 0 and len(queries) > 1:
            # Batch fallito completamente: fallback a inserimento singolo
            logger.warning(
                f"Detail batch fallito, fallback a inserimento singolo "
                f"per {len(queries)} righe"
            )
            success_count = 0
            all_errors = list(prep_errors)

            for idx, query in enumerate(queries):
                try:
                    await self.db_service.execute_detail_insert(query)
                    success_count += 1
                except Exception as e:
                    all_errors.append(f"Fallback riga {idx}: {str(e)}")

        logger.info(f"Detail rows completate: {success_count}/{len(rows)} inserite")

        if all_errors:
            return False, "; ".join(all_errors)

        return True, None


# ============= FACTORY FUNCTION =============

_importer_cache: dict[str, DynamicImporter] = {}


def get_importer(doc_type: str) -> Optional[DynamicImporter]:
    """
    Factory function per ottenere un importer

    Args:
        doc_type: Tipo documento

    Returns:
        DynamicImporter configurato o None se tipo non supportato
    """
    config = load_document_types_from_db()

    if doc_type not in config:
        logger.error(f"Tipo documento non configurato: {doc_type}")
        return None

    # Cache per evitare ri-inizializzazione
    if doc_type not in _importer_cache:
        try:
            _importer_cache[doc_type] = DynamicImporter(doc_type)
        except Exception as e:
            logger.error(f"Errore creazione importer {doc_type}: {e}")
            return None

    return _importer_cache[doc_type]


def get_all_importers() -> dict[str, DynamicImporter]:
    """Ritorna tutti gli importer disponibili"""
    config = load_document_types_from_db()
    importers = {}
    for doc_type in config.keys():
        importer = get_importer(doc_type)
        if importer:
            importers[doc_type] = importer
    return importers


def clear_importer_cache():
    """Svuota la cache degli importer (utile dopo reload config)"""
    global _importer_cache
    _importer_cache = {}

