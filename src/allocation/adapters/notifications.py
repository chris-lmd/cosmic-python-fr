"""
Adapter pour les notifications.

Ce module fournit une abstraction sur l'envoi de notifications
(emails, SMS, etc.), permettant de découpler le domaine
du mécanisme de notification concret.
"""

from __future__ import annotations

import abc
import smtplib


class AbstractNotifications(abc.ABC):
    """Interface abstraite pour les notifications."""

    @abc.abstractmethod
    def send(self, destination: str, message: str) -> None:
        raise NotImplementedError


class EmailNotifications(AbstractNotifications):
    """Implémentation concrète envoyant des emails via SMTP."""

    def __init__(self, smtp_host: str = "localhost", smtp_port: int = 587):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port

    def send(self, destination: str, message: str) -> None:
        msg = f"Subject: Notification d'allocation\n\n{message}"
        with smtplib.SMTP(self.smtp_host, self.smtp_port) as smtp:
            smtp.sendmail(
                from_addr="allocations@example.com",
                to_addrs=[destination],
                msg=msg,
            )
