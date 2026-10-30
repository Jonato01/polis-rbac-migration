"""
OCR Service
Gestisce OCR con Claude vision per rotation check e estrazione testo.

Pipeline per ogni pagina:
1. (opzionale) Pagina -> immagine -> Claude vision per determinare rotazione
2. Rotazione della singola pagina se necessaria
3. Pagina -> immagine -> Claude vision per OCR
4. Combinazione risultati di tutte le pagine

Le pagine vengono elaborate in parallelo con asyncio.gather + semaforo.
"""
import asyncio
import base64
import io
import json
import re
import logging
from dataclasses import dataclass
from typing import Optional

import fitz  # PyMuPDF
from PIL import Image

# Safety net: aumenta limite PIL per evitare DecompressionBombError
# Il vero cap e' in _page_to_image_base64 che riduce DPI dinamicamente
Image.MAX_IMAGE_PIXELS = 200_000_000

from anthropic import AsyncAnthropic

# Limite massimo pixel per pagina (larghezza * altezza).
# 4M pixel = ~2000x2000 = ~12MB RAM per pixmap RGB. Sicuro anche con 20 pagine parallele.
MAX_PAGE_PIXELS = 4_000_000

from config import (
    CLAUDE_API_KEY,
    CLAUDE_BASE_URL,
    CLAUDE_MODEL
)
from services.rate_limiter import claude_limiter

logger = logging.getLogger(__name__)


@dataclass
class OCRResult:
    """Risultato OCR con metriche qualita'"""
    provider: str
    content: str
    tables: list[dict]
    rotation_applied: int
    quality_score: float  # 0-100
    char_count: int
    word_count: int
    page_images: list[str] = None  # Immagini base64 delle pagine per vision extraction


class OCRService:
    """
    Servizio OCR con pipeline:
    1. Claude vision per check rotazione
    2. Claude vision pagina per pagina per OCR
    """

    def __init__(self):
        self.claude_client = AsyncAnthropic(
            api_key=CLAUDE_API_KEY,
            base_url=CLAUDE_BASE_URL
        )
        self.model = CLAUDE_MODEL

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def ocr_best_result(
        self,
        pdf_bytes: bytes,
        filename: str,
        check_rotation: bool = False
    ) -> Optional[OCRResult]:
        """
        Pipeline OCR parallela per ogni pagina:
        1. (opzionale) Pagina -> immagine -> Claude vision per check rotazione
        2. Rotazione della singola pagina se necessaria
        3. Pagina -> immagine -> Claude vision per OCR
        4. Combinazione risultati

        Le pagine vengono elaborate in parallelo con un semaforo per
        controllare la concorrenza massima.

        Args:
            pdf_bytes: Contenuto PDF in bytes
            filename: Nome file per logging
            check_rotation: Se True, verifica rotazione pagine via AI (default False)

        Returns:
            OCRResult con il risultato combinato, None se OCR fallito
        """
        # Dividi il PDF in pagine singole (CPU-bound, offload al thread pool)
        pages_bytes = await asyncio.to_thread(self._split_pdf_pages, pdf_bytes)
        total_pages = len(pages_bytes)
        rotation_label = "con rotation check" if check_rotation else "senza rotation check"
        logger.info(
            f"[{filename}] OCR pipeline: {total_pages} pagine da elaborare "
            f"in parallelo ({rotation_label})"
        )

        # Elabora tutte le pagine in parallelo con semaforo
        # Max 5 pagine in parallelo per contenere il picco di memoria
        # (ogni pagina usa ~15-20MB durante la conversione immagine)
        sem = asyncio.Semaphore(5)
        tasks = [
            self._process_page(page_bytes, filename, page_num, total_pages, check_rotation, sem)
            for page_num, page_bytes in enumerate(pages_bytes, 1)
        ]
        results = await asyncio.gather(*tasks)

        # Raccogli risultati mantenendo l'ordine delle pagine
        page_texts = []
        page_images = []
        rotations_applied = []
        for text, rotation, img_base64 in results:
            rotations_applied.append(rotation)
            if text:
                page_texts.append(text)
            if img_base64:
                page_images.append(img_base64)

        # Combina risultati
        if not page_texts:
            logger.error(f"[{filename}] Nessuna pagina OCR riuscita")
            return None

        # Separa le pagine con marker chiaro per aiutare l'estrazione AI
        combined_text = "\n\n--- PAGINA ---\n\n".join(page_texts)

        quality = self._calculate_ocr_quality(combined_text, filename)

        # Riassumi le rotazioni applicate
        rotations_summary = {r: rotations_applied.count(r) for r in set(rotations_applied)}
        logger.info(
            f"[{filename}] OCR completato: {total_pages} pagine, "
            f"{len(combined_text)} caratteri, qualita' {quality:.0f}%, "
            f"rotazioni: {rotations_summary}"
        )

        return OCRResult(
            provider="claude_vision",
            content=combined_text,
            tables=[],
            rotation_applied=max(set(rotations_applied), key=rotations_applied.count),
            quality_score=quality,
            char_count=len(combined_text),
            word_count=len(combined_text.split()),
            page_images=page_images if page_images else None
        )

    async def _process_page(
        self,
        page_bytes: bytes,
        filename: str,
        page_num: int,
        total_pages: int,
        check_rotation: bool,
        sem: asyncio.Semaphore
    ) -> tuple[Optional[str], int, Optional[str]]:
        """
        Elabora una singola pagina: rotation check (opzionale) + OCR.
        Usa il semaforo per limitare la concorrenza.

        Returns:
            Tupla (testo_estratto, angolo_rotazione, img_base64)
        """
        async with sem:
            rotation = 0

            if check_rotation:
                logger.info(f"[{filename}] Pagina {page_num}/{total_pages}: check rotazione...")
                rotation = await self._check_rotation_with_ai(page_bytes, filename, page_num)
                if rotation > 0:
                    logger.info(f"[{filename}] Pagina {page_num}: rotazione {rotation}")
                    page_bytes = await asyncio.to_thread(self._rotate_single_page, page_bytes, rotation)

            logger.info(f"[{filename}] Pagina {page_num}/{total_pages}: OCR...")
            text, img_base64 = await self._ocr_single_page(page_bytes, filename, page_num)

            if not text:
                logger.warning(f"[{filename}] Pagina {page_num} OCR fallito, skip")

            return (text, rotation, img_base64)

    # ==========================================
    # STEP 1: Rotation check con Claude vision
    # ==========================================

    async def _check_rotation_with_ai(
        self,
        page_bytes: bytes,
        filename: str,
        page_num: int = 1
    ) -> int:
        """
        Invia una singola pagina come immagine a Claude per determinare
        se e' ruotata e di quanti gradi.

        Args:
            page_bytes: PDF single-page in bytes
            filename: Nome file per logging
            page_num: Numero pagina (1-indexed, per logging)

        Returns:
            Angolo di rotazione (0, 90, 180, 270). 0 se non serve rotazione.
        """
        try:
            await claude_limiter.acquire()

            # Converti la pagina in immagine PNG (CPU-bound, offload al thread pool)
            img_base64 = await asyncio.to_thread(self._page_to_image_base64, page_bytes, 0, 150)
            if not img_base64:
                logger.warning(f"[{filename}] Pag {page_num}: impossibile convertire in immagine")
                return 0

            response = await self.claude_client.messages.create(
                model=self.model,
                max_tokens=100,
                temperature=0.1,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": img_base64
                            }
                        },
                        {
                            "type": "text",
                            "text": (
                                "Guarda questa pagina di documento. "
                                "Il testo e' orientato correttamente (leggibile normalmente) "
                                "oppure e' ruotato? "
                                "Rispondi SOLO in JSON (senza altro testo): "
                                '{"needs_rotation": true/false, "angle": 0} '
                                "dove angle e': 0 se normale, "
                                "90 se il testo va dal basso verso l'alto, "
                                "270 se il testo va dall'alto verso il basso, "
                                "180 se il testo e' capovolto."
                            )
                        }
                    ]
                }]
            )

            result_text = response.content[0].text
            # Prova parse diretto, poi cerca JSON nel testo
            try:
                result = json.loads(result_text)
            except json.JSONDecodeError:
                json_match = re.search(r'\{.*?\}', result_text, re.DOTALL)
                if json_match:
                    result = json.loads(json_match.group())
                else:
                    logger.warning(f"[{filename}] Pag {page_num} rotation: risposta non JSON")
                    return 0

            needs_rotation = result.get("needs_rotation", False)
            angle = int(result.get("angle", 0))

            if needs_rotation and angle in (90, 180, 270):
                logger.info(f"[{filename}] Pag {page_num} rotation check: {angle}")
                return angle

            logger.debug(f"[{filename}] Pag {page_num} rotation check: OK")
            return 0

        except Exception as e:
            logger.warning(f"[{filename}] Pag {page_num} rotation check fallito: {e}, default 0")
            return 0

    def _page_to_image_base64(
        self,
        pdf_bytes: bytes,
        page_num: int = 0,
        dpi: int = 200
    ) -> Optional[str]:
        """
        Converte una pagina PDF in immagine JPEG base64.
        Riduce DPI dinamicamente se la pagina supera MAX_PAGE_PIXELS per evitare OOM.
        Se l'immagine JPEG supera 4MB, riduce qualita' e dimensione.

        Args:
            pdf_bytes: PDF in bytes
            page_num: Numero pagina (0-indexed)
            dpi: Risoluzione immagine richiesta (verra' ridotta se necessario)

        Returns:
            Stringa base64 dell'immagine JPEG, None se fallisce
        """
        try:
            pdf_doc = fitz.open(stream=pdf_bytes, filetype="pdf")

            if page_num >= len(pdf_doc):
                pdf_doc.close()
                return None

            page = pdf_doc[page_num]

            # Calcola pixel stimati al DPI richiesto e riduci se necessario
            # page.rect e' in punti (72 DPI), quindi scala = dpi/72
            scale = dpi / 72.0
            estimated_w = int(page.rect.width * scale)
            estimated_h = int(page.rect.height * scale)
            estimated_pixels = estimated_w * estimated_h

            if estimated_pixels > MAX_PAGE_PIXELS:
                # Riduci DPI per stare sotto il limite
                reduction = (MAX_PAGE_PIXELS / estimated_pixels) ** 0.5
                dpi = int(dpi * reduction)
                logger.info(
                    f"Pagina {page_num}: {estimated_w}x{estimated_h} ({estimated_pixels/1e6:.1f}M px) "
                    f"troppo grande, ridotto DPI a {dpi}"
                )

            pix = page.get_pixmap(dpi=dpi)
            img_bytes = pix.tobytes("png")
            # Rilascia pixmap subito (~12MB per 4M pixel)
            del pix
            pdf_doc.close()

            # Converti PNG -> JPEG con Pillow
            img = Image.open(io.BytesIO(img_bytes))
            # Rilascia PNG bytes subito
            del img_bytes
            if img.mode == "RGBA":
                img = img.convert("RGB")

            MAX_SIZE = 4 * 1024 * 1024  # 4MB (limite API 5MB, margine sicurezza)

            # Prova qualita' decrescenti
            for quality in (85, 70, 50):
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=quality)
                jpeg_bytes = buf.getvalue()
                if len(jpeg_bytes) <= MAX_SIZE:
                    del img
                    result = base64.b64encode(jpeg_bytes).decode("utf-8")
                    del jpeg_bytes
                    return result

            # Se ancora troppo grande, ridimensiona (max 2048px lato maggiore)
            w, h = img.size
            max_dim = 2048
            if max(w, h) > max_dim:
                ratio = max_dim / max(w, h)
                img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=50)
            jpeg_bytes = buf.getvalue()
            del img
            logger.info(
                f"Pagina {page_num} ridimensionata: {len(jpeg_bytes) / 1024:.0f}KB"
            )
            result = base64.b64encode(jpeg_bytes).decode("utf-8")
            del jpeg_bytes
            return result

        except Exception as e:
            logger.warning(f"Errore conversione pagina {page_num} in immagine: {e}")
            return None

    # ==========================================
    # STEP 3: OCR pagina per pagina con Claude vision
    # ==========================================

    def _split_pdf_pages(self, pdf_bytes: bytes) -> list[bytes]:
        """
        Divide un PDF multi-pagina in una lista di PDF single-page.

        Args:
            pdf_bytes: PDF completo in bytes

        Returns:
            Lista di bytes, ciascuno un PDF di una singola pagina
        """
        pdf_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages = []

        for page_num in range(len(pdf_doc)):
            single_page_pdf = fitz.open()
            single_page_pdf.insert_pdf(pdf_doc, from_page=page_num, to_page=page_num)

            page_bytes = single_page_pdf.tobytes()
            single_page_pdf.close()
            pages.append(page_bytes)

        pdf_doc.close()

        logger.debug(f"PDF diviso in {len(pages)} pagine")
        return pages

    async def _ocr_single_page(
        self,
        page_bytes: bytes,
        filename: str,
        page_num: int
    ) -> tuple[Optional[str], Optional[str]]:
        """
        OCR di una singola pagina con Claude vision.

        Args:
            page_bytes: PDF single-page in bytes
            filename: Nome file per logging
            page_num: Numero pagina (1-indexed, per logging)

        Returns:
            Tupla (testo_estratto, img_base64). img_base64 conservata per vision extraction.
        """
        try:
            await claude_limiter.acquire()

            # Converti pagina in PNG base64 (CPU-bound, offload al thread pool)
            img_base64 = await asyncio.to_thread(self._page_to_image_base64, page_bytes, 0, 200)
            if not img_base64:
                logger.warning(
                    f"[{filename}] Pagina {page_num}: impossibile convertire in immagine"
                )
                return None, None

            response = await self.claude_client.messages.create(
                model=self.model,
                max_tokens=8192,
                temperature=0.0,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": img_base64
                            }
                        },
                        {
                            "type": "text",
                            "text": (
                                "Estrai TUTTO il testo visibile in questa immagine di documento.\n"
                                "Mantieni la struttura originale: tabelle, elenchi, intestazioni.\n"
                                "Per le tabelle, usa il formato:\n"
                                "Colonna1 | Colonna2 | Colonna3\n"
                                "valore1 | valore2 | valore3\n"
                                "Trascrivi i numeri ESATTAMENTE come appaiono (inclusi separatori decimali).\n"
                                "Restituisci SOLO il testo estratto, senza commenti."
                            )
                        }
                    ]
                }]
            )

            text = response.content[0].text
            logger.debug(
                f"[{filename}] Pagina {page_num}: {len(text)} caratteri estratti"
            )
            return text, img_base64

        except Exception as e:
            logger.warning(
                f"[{filename}] Pagina {page_num} OCR errore: {e}"
            )
            return None, None

    # ==========================================
    # Helpers comuni
    # ==========================================

    def _calculate_ocr_quality(self, text: str, filename: str) -> float:
        """
        Calcola score qualita' OCR (0-100)

        Considera:
        - Quantita' testo estratto
        - Presenza pattern attesi (date, importi, parole chiave)
        - Coerenza con nome file
        """
        if not text:
            return 0.0

        score = 0.0

        # 1. Quantita' caratteri alfanumerici (max 25 punti)
        alpha_chars = sum(1 for c in text if c.isalnum())
        score += min(25, alpha_chars / 40)  # 1000+ char = 25 punti

        # 2. Quantita' parole (max 25 punti)
        words = len(text.split())
        score += min(25, words / 8)  # 200+ parole = 25 punti

        # 3. Pattern documenti italiani (max 30 punti)
        patterns = [
            (r'\d{2}[/.-]\d{2}[/.-]\d{4}', 6),      # Date DD/MM/YYYY
            (r'\u20ac?\s*[\d.,]+[.,]\d{2}', 6),      # Importi
            (r'\b[A-Z]{2}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]\b', 6),  # Codice fiscale
            (r'\b\d{11}\b', 4),                      # Partita IVA
            (r'fattura|contratto|accordo|incarico|timesheet|verbale', 4),  # Keyword
            (r'totale|importo|data|numero|oggetto', 4),  # Keyword comuni
        ]

        for pattern, points in patterns:
            if re.search(pattern, text, re.IGNORECASE):
                score += points

        # 4. Coerenza con nome file (max 20 punti)
        filename_lower = filename.lower()
        text_lower = text.lower()

        filename_words = re.findall(r'[a-z]{3,}', filename_lower)
        matches = sum(1 for word in filename_words if word in text_lower)
        score += min(20, matches * 5)

        return min(100, score)

    def _rotate_single_page(self, page_bytes: bytes, angle: int) -> bytes:
        """
        Ruota una singola pagina PDF dell'angolo specificato.

        Args:
            page_bytes: PDF single-page in bytes
            angle: Angolo di rotazione (90, 180, 270)

        Returns:
            PDF single-page ruotato in bytes
        """
        pdf_document = fitz.open(stream=page_bytes, filetype="pdf")
        page = pdf_document[0]
        page.set_rotation((page.rotation + angle) % 360)

        output = io.BytesIO()
        pdf_document.save(output)
        pdf_document.close()

        return output.getvalue()
