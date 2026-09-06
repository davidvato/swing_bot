import logging
import requests
from concurrent.futures import ThreadPoolExecutor
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

class TelegramNotifier:
    """
    Notificador asincrono para enviar mensajes a Telegram.
    Utiliza un ThreadPoolExecutor para no bloquear el hilo principal.
    """
    def __init__(self):
        self.bot_token = TELEGRAM_BOT_TOKEN
        self.chat_id = TELEGRAM_CHAT_ID
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.enabled = bool(self.bot_token and self.chat_id)
        
        if not self.enabled:
            logger.warning("TelegramNotifier deshabilitado: Faltan credenciales en .env")
        else:
            logger.info("TelegramNotifier inicializado correctamente.")

    def send_message(self, text: str) -> None:
        """
        Envia un mensaje a Telegram de forma no bloqueante.
        """
        if not self.enabled:
            return

        self.executor.submit(self._send_request, text)

    def _send_request(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
        except Exception as e:
            logger.error(f"Error al enviar mensaje a Telegram: {e}")

    def shutdown(self):
        """Cierra el executor limpiamente."""
        self.executor.shutdown(wait=False)
