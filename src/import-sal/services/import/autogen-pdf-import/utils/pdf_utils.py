"""
PDF Utilities
Gestione PDF, P7M e conversione DOCX
"""
import io
import os
import subprocess
import tempfile
import logging

from asn1crypto import cms

logger = logging.getLogger(__name__)

MIN_VALID_FILE_SIZE = 1024  # 1 KB - sotto questa soglia il file è considerato corrotto


def is_p7m_file(filename: str) -> bool:
    """Verifica se il file è un P7M"""
    return filename.lower().endswith('.p7m')


def is_pdf_file(filename: str) -> bool:
    """Verifica se il file è un PDF"""
    return filename.lower().endswith('.pdf')


def is_docx_file(filename: str) -> bool:
    """Verifica se il file è un DOCX"""
    return filename.lower().endswith('.docx')


def is_docx_content(content: bytes) -> bool:
    """Verifica se il contenuto è un DOCX (ZIP con struttura Office)"""
    return len(content) > 100 and content[:2] == b'PK'


def is_p7m_content(content: bytes) -> bool:
    """Verifica se il contenuto è un altro P7M (struttura ASN.1/DER)"""
    return len(content) > 2 and content[0] == 0x30  # SEQUENCE tag


def convert_docx_to_pdf(docx_bytes: bytes) -> bytes | None:
    """
    Converte DOCX in PDF usando LibreOffice headless

    Args:
        docx_bytes: Contenuto file DOCX

    Returns:
        PDF in bytes o None se conversione fallisce
    """
    tmp_dir = None
    try:
        tmp_dir = tempfile.mkdtemp()
        docx_path = os.path.join(tmp_dir, "input.docx")

        with open(docx_path, 'wb') as f:
            f.write(docx_bytes)

        result = subprocess.run(
            ['libreoffice', '--headless', '--convert-to', 'pdf', '--outdir', tmp_dir, docx_path],
            capture_output=True,
            timeout=60
        )

        pdf_path = os.path.join(tmp_dir, "input.pdf")
        if result.returncode == 0 and os.path.exists(pdf_path):
            with open(pdf_path, 'rb') as f:
                pdf_bytes = f.read()
            if pdf_bytes[:4] == b'%PDF':
                logger.info("DOCX convertito in PDF con successo")
                return pdf_bytes

        logger.warning(f"Conversione DOCX→PDF fallita: {result.stderr.decode(errors='replace')}")
        return None

    except subprocess.TimeoutExpired:
        logger.error("LibreOffice timeout durante conversione DOCX→PDF")
        return None
    except FileNotFoundError:
        logger.error("LibreOffice non installato")
        return None
    except Exception as e:
        logger.error(f"Errore conversione DOCX→PDF: {e}")
        return None
    finally:
        if tmp_dir:
            for fname in os.listdir(tmp_dir):
                try:
                    os.remove(os.path.join(tmp_dir, fname))
                except:
                    pass
            try:
                os.rmdir(tmp_dir)
            except:
                pass


def extract_content_from_p7m_asn1(p7m_bytes: bytes) -> bytes | None:
    """
    Estrae contenuto raw da P7M usando parsing ASN.1 puro Python.
    Non valida il tipo di contenuto (PDF, DOCX, altro P7M).

    Args:
        p7m_bytes: Contenuto file P7M

    Returns:
        Bytes grezzi del contenuto o None se fallisce
    """
    try:
        content_info = cms.ContentInfo.load(p7m_bytes)

        if content_info['content_type'].native != 'signed_data':
            logger.error("P7M non è signed_data")
            return None

        signed_data = content_info['content']
        encap_content = signed_data['encap_content_info']

        if encap_content['content'] is not None:
            raw_bytes = encap_content['content'].native
            logger.debug(f"Contenuto estratto via asn1crypto ({len(raw_bytes)} bytes, primi bytes: {raw_bytes[:4].hex()})")
            return raw_bytes

        return None

    except Exception as e:
        logger.warning(f"Errore parsing ASN.1: {e}")
        return None


def extract_content_from_p7m_openssl(p7m_bytes: bytes) -> bytes | None:
    """
    Estrae contenuto raw da P7M usando OpenSSL.
    Non valida il tipo di contenuto.

    Args:
        p7m_bytes: Contenuto file P7M

    Returns:
        Bytes grezzi del contenuto o None se fallisce
    """
    p7m_path = None
    out_path = None

    try:
        with tempfile.NamedTemporaryFile(suffix='.p7m', delete=False) as tmp_p7m:
            tmp_p7m.write(p7m_bytes)
            p7m_path = tmp_p7m.name

        out_path = p7m_path.replace('.p7m', '.out')

        result = subprocess.run(
            ['openssl', 'smime', '-verify', '-noverify', '-in', p7m_path, '-out', out_path],
            capture_output=True,
            timeout=30
        )

        if result.returncode == 0 and os.path.exists(out_path):
            with open(out_path, 'rb') as f:
                raw_bytes = f.read()
            logger.debug(f"Contenuto estratto via OpenSSL ({len(raw_bytes)} bytes)")
            return raw_bytes

        logger.warning(f"OpenSSL estrazione fallita: {result.stderr.decode(errors='replace')}")
        return None

    except subprocess.TimeoutExpired:
        logger.error("OpenSSL timeout")
        return None
    except FileNotFoundError:
        logger.warning("OpenSSL non installato")
        return None
    except Exception as e:
        logger.error(f"Errore OpenSSL: {e}")
        return None
    finally:
        for path in [p7m_path, out_path]:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except:
                    pass


def extract_pdf_from_p7m(p7m_bytes: bytes, max_depth: int = 3) -> bytes | None:
    """
    Estrae PDF da P7M, gestendo DOCX e P7M ricorsivi.

    Args:
        p7m_bytes: Contenuto file P7M
        max_depth: Profondità massima ricorsione per P7M annidati

    Returns:
        PDF in bytes o None se fallisce
    """
    # 1. Estrai raw content
    content = extract_content_from_p7m_asn1(p7m_bytes)
    if not content:
        logger.info("asn1crypto fallito, provo OpenSSL...")
        content = extract_content_from_p7m_openssl(p7m_bytes)
    if not content:
        return None

    # 2. Se è PDF → ritorna
    if content[:4] == b'%PDF':
        logger.debug("Contenuto P7M è PDF")
        return content

    # 3. Se è un altro P7M → ricorsione
    if is_p7m_content(content) and max_depth > 0:
        logger.info(f"Contenuto P7M è un altro P7M, ricorsione (depth rimanente: {max_depth - 1})")
        return extract_pdf_from_p7m(content, max_depth - 1)

    # 4. Se è DOCX → converti a PDF
    if is_docx_content(content):
        logger.info("Contenuto P7M è DOCX, conversione in PDF...")
        return convert_docx_to_pdf(content)

    # 5. Tipo non riconosciuto
    logger.warning(f"Contenuto P7M non riconosciuto (primi bytes: {content[:8].hex()})")
    return None


def get_pdf_content(file_bytes: bytes, filename: str) -> tuple[bytes | None, str | None]:
    """
    Ottiene contenuto PDF, gestendo P7M, DOCX e file corrotti.

    Args:
        file_bytes: Contenuto file originale
        filename: Nome file

    Returns:
        Tupla (pdf_bytes, error_reason):
        - (bytes, None) → successo
        - (None, "corrupted") → file corrotto
        - (None, "extraction_failed") → estrazione P7M/DOCX fallita
        - (None, "unsupported") → tipo file non supportato
    """
    # Check file corrotto (< 1 KB)
    if len(file_bytes) < MIN_VALID_FILE_SIZE:
        logger.warning(f"File corrotto (dimensione: {len(file_bytes)} bytes): {filename}")
        return None, "corrupted"

    if is_p7m_file(filename):
        logger.info(f"Estrazione PDF da P7M: {filename}")
        pdf = extract_pdf_from_p7m(file_bytes)
        return (pdf, None) if pdf else (None, "extraction_failed")
    elif is_pdf_file(filename):
        if file_bytes[:4] == b'%PDF':
            return file_bytes, None
        else:
            logger.warning(f"File {filename} ha estensione .pdf ma non è un PDF valido")
            return None, "corrupted"
    elif is_docx_file(filename):
        logger.info(f"Conversione DOCX in PDF: {filename}")
        pdf = convert_docx_to_pdf(file_bytes)
        return (pdf, None) if pdf else (None, "extraction_failed")
    else:
        logger.warning(f"Tipo file non supportato: {filename}")
        return None, "unsupported"
