"""
Bridge Anthropic SDK -> Azure AI Foundry.

Azure AI Foundry espone un endpoint compatibile con l'API Anthropic.
L'SDK standard funziona direttamente puntando la base_url verso Azure.

Uso:
  - call_ai(prompt) per chiamate dirette (classificazione, rendicontazione)
  - patch_anthropic_module(mod.anthropic) per monkey-patch degli script SAL-7
"""
import logging

import anthropic

from container_app import config

logger = logging.getLogger("container_app.azure_ai_bridge")

# Salva riferimento alla classe originale prima di qualsiasi monkey-patch
_OriginalAnthropic = anthropic.Anthropic


def _get_base_url() -> str:
    """
    Ricava la base_url per il SDK Anthropic dall'endpoint Azure AI.
    L'SDK aggiunge da solo '/v1/messages', quindi serve solo il prefisso.
    Es: 'https://host/anthropic/v1/messages' -> 'https://host/anthropic'
    """
    endpoint = config.AZURE_AI_ENDPOINT.rstrip("/")
    if endpoint.endswith("/v1/messages"):
        return endpoint[: -len("/v1/messages")]
    return endpoint


class AzureAIClient:
    """
    Drop-in replacement per anthropic.Anthropic.
    Ignora l'api_key passata dallo script e usa le env vars Azure.
    Restituisce oggetti nativi del SDK (Message, TextBlock, etc.).
    """

    def __init__(self, api_key: str = "", **kwargs):
        self._client = _OriginalAnthropic(
            api_key=config.AZURE_AI_API_KEY,
            base_url=_get_base_url(),
        )
        self.messages = self._client.messages
        logger.info(
            "AzureAIClient inizializzato: endpoint=%s, deployment=%s",
            config.AZURE_AI_ENDPOINT,
            config.AZURE_AI_DEPLOYMENT_NAME,
        )


def patch_anthropic_module(module):
    """
    Monkey-patch: sostituisce anthropic.Anthropic nel modulo importato,
    cosi' le chiamate vengono redirette su Azure AI Foundry.

    NON patcha anthropic.APIError: il SDK reale genera errori reali
    che gli script catturano correttamente.

    Uso:
        import SAL_7.import_fatture_sal7 as mod
        patch_anthropic_module(mod.anthropic)
    """
    module.Anthropic = AzureAIClient
    logger.info("Modulo anthropic patchato -> Azure AI Foundry")


def call_ai(prompt: str, system_message: str = "", max_tokens: int = 4096) -> str:
    """
    Chiamata diretta all'AI Azure per uso generico.
    Usata da file_classifier e rendicontazione_adapter.
    Restituisce il testo della risposta.
    """
    client = _OriginalAnthropic(
        api_key=config.AZURE_AI_API_KEY,
        base_url=config.AZURE_AI_ENDPOINT,
    )

    kwargs = {
        "model": config.AZURE_AI_DEPLOYMENT_NAME,
        "max_tokens": max_tokens,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system_message:
        kwargs["system"] = system_message

    response = client.messages.create(**kwargs)

    if response.content:
        return response.content[0].text
    return ""
