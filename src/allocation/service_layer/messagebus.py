"""
Message Bus.

Le message bus est le point central de dispatch des messages
(commands et events) vers leurs handlers respectifs.

Fonctionnement :
1. Un message (command ou event) entre dans le bus
2. Le bus trouve le(s) handler(s) correspondant(s)
3. Le handler est exécuté
4. Les événements émis pendant l'exécution sont collectés et traités à leur tour

Différences clés :
- Une command a exactement UN handler ; l'erreur remonte à l'appelant
- Un event peut avoir 0 à N handlers ; les erreurs sont loggées mais ne bloquent pas
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, Union

from allocation.domain import commands, events
from allocation.service_layer import unit_of_work

logger = logging.getLogger(__name__)

Message = Union[commands.Command, events.Event]


class MessageBus:
    """
    Message Bus avec injection de dépendances.

    Les dépendances (uow, notifications, etc.) sont injectées
    à la construction et transmises automatiquement aux handlers
    par introspection de leurs signatures.
    """

    def __init__(
        self,
        uow: unit_of_work.AbstractUnitOfWork,
        event_handlers: dict[type[events.Event], list[Callable]],
        command_handlers: dict[type[commands.Command], Callable],
        dependencies: dict[str, Any] | None = None,
    ):
        self.uow = uow
        self.event_handlers = event_handlers
        self.command_handlers = command_handlers
        self.dependencies = dependencies or {}
        self.queue: list[Message] = []

    def handle(self, message: Message) -> list[Any]:
        """
        Point d'entrée principal : traite un message et tous
        les événements qui en découlent (propagation en cascade).

        La queue interne accumule les events émis par les handlers ;
        le bus les traite un par un jusqu'à vider la queue.
        """
        self.queue = [message]
        results: list[Any] = []
        while self.queue:
            message = self.queue.pop(0)
            if isinstance(message, events.Event):
                self._handle_event(message)
            elif isinstance(message, commands.Command):
                result = self._handle_command(message)
                results.append(result)
            else:
                raise ValueError(f"Message de type inconnu : {type(message)}")
        return results

    def _handle_event(self, event: events.Event) -> None:
        """
        Dispatch un event vers tous ses handlers.

        Si un handler échoue, l'erreur est loggée mais les
        autres handlers continuent (tolérance aux pannes).
        """
        for handler in self.event_handlers.get(type(event), []):
            try:
                logger.debug("Traitement de l'event %s avec %s", event, handler)
                self._call_handler(handler, event)
                self.queue.extend(self.uow.collect_new_events())
            except Exception:
                logger.exception("Erreur lors du traitement de l'event %s", event)

    def _handle_command(self, command: commands.Command) -> Any:
        """
        Dispatch une command vers son unique handler.

        Contrairement aux events, une erreur de command remonte
        directement à l'appelant (pas de tolérance).
        """
        logger.debug("Traitement de la command %s", command)
        handler = self.command_handlers.get(type(command))
        if handler is None:
            raise ValueError(f"Aucun handler pour la command {type(command)}")
        result = self._call_handler(handler, command)
        self.queue.extend(self.uow.collect_new_events())
        return result

    def _call_handler(self, handler: Callable, message: Message) -> Any:
        """
        Appelle un handler en injectant les dépendances nécessaires.

        Introspection : on lit la signature du handler pour déterminer
        quelles dépendances il attend. Le premier paramètre est toujours
        le message lui-même ; les suivants sont résolus par nom
        dans le dictionnaire de dépendances ou via self.uow.
        """
        params = inspect.signature(handler).parameters
        kwargs: dict[str, Any] = {}
        for name, param in params.items():
            if name == list(params.keys())[0]:
                # Premier paramètre = le message lui-même, on le passe en positional
                continue
            if name == "uow":
                kwargs[name] = self.uow
            elif name in self.dependencies:
                kwargs[name] = self.dependencies[name]

        return handler(message, **kwargs)
