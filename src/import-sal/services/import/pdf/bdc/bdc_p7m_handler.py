"""
bdc_p7m_handler.py - Gestione file P7M per BDC
Estrae PDF da file P7M firmati digitalmente
"""
import tempfile
import os
import sys
# Aggiungi parent directory al path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from pathlib import Path
from typing import Tuple, Optional



def is_p7m_file(filename: str) -> bool:
    """Verifica se un file è un P7M"""
    return filename.lower().endswith('.p7m')


def extract_pdf_from_p7m(p7m_path: str, output_path: Optional[str] = None) -> str:
    """
    Estrae PDF da file P7M usando asn1crypto
    
    Args:
        p7m_path: Percorso file P7M
        output_path: Percorso output opzionale
    
    Returns:
        Percorso del PDF estratto
    
    Raises:
        ImportError: Se asn1crypto non installata
        ValueError: Se il file non contiene PDF valido
    """
    try:
        from asn1crypto import cms
    except ImportError:
        raise ImportError(
            "asn1crypto non installato. Esegui: pip install asn1crypto"
        )
    
    print(f" Estrazione P7M: {Path(p7m_path).name}")
    
    # Leggi P7M
    with open(p7m_path, 'rb') as f:
        p7m_data = f.read()
    
    print(f" Dimensione P7M: {len(p7m_data):,} bytes")
    
    try:
        # Parse PKCS#7/CMS
        content_info = cms.ContentInfo.load(p7m_data)
        
        if content_info['content_type'].native != 'signed_data':
            raise ValueError(
                f"Tipo contenuto non supportato: {content_info['content_type'].native}"
            )
        
        signed_data = content_info['content']
        encap_content_info = signed_data['encap_content_info']
        
        if encap_content_info['content'] is None:
            raise ValueError("Nessun contenuto nel P7M")
        
        # Estrai contenuto
        content = encap_content_info['content']
        
        if hasattr(content, 'native'):
            pdf_bytes = content.native
        elif hasattr(content, 'dump'):
            pdf_bytes = content.dump()
        elif hasattr(content, 'contents'):
            pdf_bytes = content.contents
        else:
            pdf_bytes = bytes(content)
        
        print(f" Contenuto estratto: {len(pdf_bytes):,} bytes")
        
    except Exception as e:
        print(f" Parsing CMS fallito, uso estrazione raw")
        # Fallback: estrazione raw
        pdf_bytes = _extract_pdf_raw(p7m_data)
    
    # Verifica header PDF
    if not pdf_bytes.startswith(b'%PDF'):
        pdf_start = pdf_bytes.find(b'%PDF')
        if pdf_start > 0:
            print(f" Rimozione header non-PDF ({pdf_start} bytes)")
            pdf_bytes = pdf_bytes[pdf_start:]
        else:
            raise ValueError("Contenuto estratto non è un PDF valido")
    
    # Verifica EOF
    if b'%%EOF' in pdf_bytes:
        eof_pos = pdf_bytes.rfind(b'%%EOF')
        end_pos = eof_pos + len(b'%%EOF')
        while end_pos < len(pdf_bytes) and pdf_bytes[end_pos:end_pos+1] in (b'\n', b'\r'):
            end_pos += 1
        pdf_bytes = pdf_bytes[:end_pos]
    
    # Determina output path
    if output_path is None:
        # Crea file temporaneo
        temp_fd, output_path = tempfile.mkstemp(suffix='.pdf', prefix='bdc_p7m_')
        os.close(temp_fd)
    
    # Scrivi PDF
    with open(output_path, 'wb') as f:
        f.write(pdf_bytes)
    
    print(f" PDF estratto: {len(pdf_bytes):,} bytes")
    
    return output_path


def _extract_pdf_raw(p7m_data: bytes) -> bytes:
    """Metodo fallback per estrazione raw"""
    pdf_start = p7m_data.find(b'%PDF')
    if pdf_start == -1:
        raise ValueError("PDF non trovato nel P7M")
    
    pdf_end = p7m_data.rfind(b'%%EOF')
    if pdf_end != -1:
        pdf_end += len(b'%%EOF')
        while pdf_end < len(p7m_data) and p7m_data[pdf_end:pdf_end+1] in (b'\n', b'\r'):
            pdf_end += 1
        return p7m_data[pdf_start:pdf_end]
    else:
        return p7m_data[pdf_start:]


def process_bdc_file(file_path: str) -> Tuple[str, bool]:
    """
    Processa file BDC (PDF o P7M)
    
    Args:
        file_path: Percorso del file
    
    Returns:
        Tuple: (percorso_pdf, is_temp_file)
            - percorso_pdf: Path del PDF processabile
            - is_temp_file: True se temporaneo (va eliminato dopo uso)
    
    Raises:
        ValueError: Se formato non supportato
    """
    if file_path.lower().endswith('.p7m'):
        # Estrai PDF da P7M
        pdf_path = extract_pdf_from_p7m(file_path)
        return pdf_path, True # File temporaneo
    
    elif file_path.lower().endswith('.pdf'):
        # È già un PDF
        return file_path, False
    
    else:
        raise ValueError(f"Formato non supportato: {file_path}")


def cleanup_temp_file(file_path: str):
    """Elimina file temporaneo in modo sicuro"""
    try:
        Path(file_path).unlink(missing_ok=True)
        print(f"File temporaneo eliminato")
    except Exception as e:
        print(f"Impossibile eliminare temp file: {e}")