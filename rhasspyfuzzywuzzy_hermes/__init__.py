"""Hermes MQTT server for Rhasspy fuzzywuzzy"""
import io
import json
import logging
import re
import time
import typing
from collections import defaultdict
from pathlib import Path

import attr
import fuzzywuzzy.process
import networkx as nx
import rhasspynlu
from rhasspyhermes.base import Message
from rhasspyhermes.intent import Intent, Slot, SlotRange
from rhasspyhermes.nlu import (
    NluError,
    NluIntent,
    NluIntentNotRecognized,
    NluQuery,
    NluTrain,
    NluTrainSuccess,
)
from rhasspynlu.intent import Recognition
from rhasspynlu.jsgf import Sentence

from .utils import generate_examples

_LOGGER = logging.getLogger(__name__)

# -----------------------------------------------------------------------------


class NluHermesMqtt:
    """Hermes MQTT server for Rhasspy fuzzywuzzy."""

    def __init__(
        self,
        client,
        intent_graph: typing.Optional[nx.DiGraph] = None,
        intent_graph_path: typing.Optional[Path] = None,
        examples: typing.Optional[typing.Dict[str, typing.List[int]]] = None,
        examples_path: typing.Optional[Path] = None,
        sentences: typing.Optional[typing.List[Path]] = None,
        slots_dirs: typing.Optional[typing.List[Path]] = None,
        slot_programs_dirs: typing.Optional[typing.List[Path]] = None,
        default_entities: typing.Dict[str, typing.Iterable[Sentence]] = None,
        language: str = "en",
        siteIds: typing.Optional[typing.List[str]] = None,
    ):
        self.client = client

        # Intent graph
        self.intent_graph = intent_graph
        self.intent_graph_path = intent_graph_path

        # Examples
        self.examples = examples
        self.examples_path = examples_path

        self.sentences = sentences or []
        self.default_entities = default_entities or {}

        # Slots
        self.slots_dirs = slots_dirs or []
        self.slot_programs_dirs = slot_programs_dirs or []

        self.siteIds = siteIds or []
        self.language = language

    # -------------------------------------------------------------------------

    def handle_query(self, query: NluQuery):
        """Do intent recognition."""
        # Check intent graph
        if (
            not self.intent_graph
            and self.intent_graph_path
            and self.intent_graph_path.is_file()
        ):
            # Load intent graph from file
            _LOGGER.debug("Loading graph from %s", str(self.intent_graph_path))
            with open(self.intent_graph_path, "r") as intent_graph_file:
                graph_dict = json.load(intent_graph_file)
                self.intent_graph = rhasspynlu.json_to_graph(graph_dict)

        # Check examples
        if not self.examples and self.examples_path and self.examples_path.is_file():
            # Load examples from file
            _LOGGER.debug("Loading examples from %s", str(self.examples_path))
            with open(self.examples_path, "r") as examples_file:
                self.examples = json.load(examples_file)

        if self.intent_graph and self.examples:

            def intent_filter(intent_name: str) -> bool:
                """Filter out intents."""
                if query.intentFilter:
                    return intent_name in query.intentFilter
                return True

            recognitions = NluHermesMqtt.recognize(
                query.input,
                self.intent_graph,
                self.examples,
                intent_filter=intent_filter,
                language=self.language,
            )
        else:
            _LOGGER.error("No intent graph or examples loaded")
            recognitions = []

        if recognitions:
            # Use first recognition only.
            recognition = recognitions[0]
            assert recognition is not None
            assert recognition.intent is not None

            self.publish(
                NluIntent(
                    input=query.input,
                    id=query.id,
                    siteId=query.siteId,
                    sessionId=query.sessionId,
                    intent=Intent(
                        intentName=recognition.intent.name,
                        confidenceScore=recognition.intent.confidence,
                    ),
                    slots=[
                        Slot(
                            entity=e.entity,
                            slotName=e.entity,
                            confidence=1,
                            value=e.value,
                            raw_value=e.raw_value,
                            range=SlotRange(start=e.raw_start, end=e.raw_end),
                        )
                        for e in recognition.entities
                    ],
                ),
                intentName=recognition.intent.name,
            )
        else:
            # Not recognized
            self.publish(
                NluIntentNotRecognized(
                    input=query.input,
                    id=query.id,
                    siteId=query.siteId,
                    sessionId=query.sessionId,
                )
            )

    # -------------------------------------------------------------------------

    @classmethod
    def recognize(
        cls,
        input_text: str,
        intent_graph: nx.DiGraph,
        examples: typing.Dict[str, typing.Dict[str, typing.List[int]]],
        intent_filter: typing.Optional[typing.Callable[[str], bool]] = None,
        replace_numbers: bool = True,
        language: str = "en",
        extra_converters: typing.Optional[
            typing.Dict[str, typing.Callable[..., typing.Any]]
        ] = None,
    ) -> typing.List[Recognition]:
        """Find the closest matching intent(s)."""
        start_time = time.perf_counter()

        if replace_numbers:
            # 75 -> seventy five
            words = rhasspynlu.numbers.replace_numbers(
                input_text.split(), language=language
            )

            input_text = " ".join(words)

        # TODO: Add cache
        intent_filter = intent_filter or (lambda i: True)
        choices: typing.Dict[str, typing.List[int]] = {
            text: path
            for intent_name, paths in examples.items()
            for text, path in paths.items()
            if intent_filter(intent_name)
        }

        # Find closest match
        best_text, best_score = fuzzywuzzy.process.extractOne(
            input_text, choices.keys()
        )
        _LOGGER.debug("input=%s, match=%s, score=%s", input_text, best_text, best_score)
        best_path = choices[best_text]

        end_time = time.perf_counter()
        _, recognition = rhasspynlu.fsticuffs.path_to_recognition(
            best_path, intent_graph, extra_converters=extra_converters
        )

        assert recognition
        recognition.intent.confidence = best_score / 100
        recognition.recognize_seconds = end_time - start_time
        recognition.raw_text = input_text
        recognition.raw_tokens = input_text.split()

        return [recognition]

    # -------------------------------------------------------------------------

    def handle_train(
        self, message: NluTrain, siteId: str = "default"
    ) -> typing.Union[NluTrainSuccess, NluError]:
        """Transform sentences to intent examples"""
        _LOGGER.debug("<- %s", message)

        try:
            self.intent_graph, self.examples = NluHermesMqtt.train(
                message.sentences,
                intent_graph_path=self.intent_graph_path,
                examples_path=self.examples_path,
                slots_dirs=self.slots_dirs,
                slot_programs_dirs=self.slot_programs_dirs,
            )

            return NluTrainSuccess(id=message.id)
        except Exception as e:
            _LOGGER.exception("handle_train")
            return NluError(siteId=siteId, error=str(e), context=message.id)

    # -------------------------------------------------------------------------

    @classmethod
    def train(
        cls,
        sentences: typing.Dict[str, str],
        intent_graph_path: typing.Optional[Path] = None,
        examples_path: typing.Optional[Path] = None,
        slots_dirs: typing.Optional[typing.List[Path]] = None,
        slot_programs_dirs: typing.Optional[typing.List[Path]] = None,
        replace_numbers: bool = True,
        word_casing="ignore",
    ) -> typing.Tuple[nx.DiGraph, typing.Dict[str, typing.Dict[str, typing.List[int]]]]:
        """Transform sentences to an intent graph and examples"""
        # Parse sentences and convert to graph
        with io.StringIO() as ini_file:
            # Join as single ini file
            for lines in sentences.values():
                print(lines, file=ini_file)
                print("", file=ini_file)

            # Parse JSGF sentences
            intents = rhasspynlu.parse_ini(ini_file.getvalue())

        # Split into sentences and rule/slot replacements
        sentences, replacements = rhasspynlu.ini_jsgf.split_rules(intents)

        if replace_numbers:
            # Replace number ranges with slot references
            for intent_sentences in sentences.values():
                for sentence in intent_sentences:
                    rhasspynlu.jsgf.walk_expression(
                        sentence, rhasspynlu.number_range_transform, replacements
                    )

        # Load slot values
        # TODO: Add word_transform
        slot_replacements = rhasspynlu.get_slot_replacements(
            intents, slots_dirs=slots_dirs, slot_programs_dirs=slot_programs_dirs
        )

        # Merge with existing replacements
        for slot_key, slot_values in slot_replacements.items():
            replacements[slot_key] = slot_values

        if replace_numbers:
            # Do single number transformations
            for intent_sentences in sentences.values():
                for sentence in intent_sentences:
                    rhasspynlu.jsgf.walk_expression(
                        sentence, rhasspynlu.number_transform, replacements
                    )

        # Convert to directed graph
        intent_graph = rhasspynlu.intents_to_graph(sentences, replacements=replacements)
        if intent_graph_path:
            with open(intent_graph_path, "w") as graph_file:
                graph_dict = rhasspynlu.graph_to_json(intent_graph)
                json.dump(graph_dict, graph_file)

            _LOGGER.debug("Wrote %s", str(intent_graph_path))

        # Generate all possible intents
        _LOGGER.debug("Generating examples")
        examples: typing.Dict[str, typing.Dict[str, typing.List[int]]] = defaultdict(
            dict
        )
        for intent_name, words, path in generate_examples(intent_graph):
            sentence = " ".join(words)
            examples[intent_name][sentence] = path

        _LOGGER.debug("Examples generated")

        if examples_path:
            # Write to JSON file
            with open(examples_path, "w") as examples_file:
                json.dump(examples, examples_file)

            _LOGGER.debug("Wrote %s", str(examples_path))

        return (intent_graph, examples)

    # -------------------------------------------------------------------------

    def on_connect(self, client, userdata, flags, rc):
        """Connected to MQTT broker."""
        try:
            topics = [NluQuery.topic()]

            if self.siteIds:
                # Specific siteIds
                topics.extend(
                    [NluTrain.topic(siteId=siteId) for siteId in self.siteIds]
                )
            else:
                # All siteIds
                topics.append(NluTrain.topic(siteId="+"))

            for topic in topics:
                self.client.subscribe(topic)
                _LOGGER.debug("Subscribed to %s", topic)
        except Exception:
            _LOGGER.exception("on_connect")

    def on_message(self, client, userdata, msg):
        """Received message from MQTT broker."""
        try:
            _LOGGER.debug("Received %s byte(s) on %s", len(msg.payload), msg.topic)
            if msg.topic == NluQuery.topic():
                json_payload = json.loads(msg.payload)

                # Check siteId
                if not self._check_siteId(json_payload):
                    return

                try:
                    query = NluQuery(**json_payload)
                    _LOGGER.debug("<- %s", query)
                    self.handle_query(query)
                except Exception as e:
                    _LOGGER.exception("nlu query")
                    self.publish(
                        NluError(
                            siteId=query.siteId,
                            sessionId=json_payload.get("sessionId", ""),
                            error=str(e),
                            context="",
                        )
                    )
            elif NluTrain.is_topic(msg.topic):
                siteId = NluTrain.get_siteId(msg.topic)
                if self.siteIds and (siteId not in self.siteIds):
                    return

                json_payload = json.loads(msg.payload)
                train = NluTrain(**json_payload)
                result = self.handle_train(train)
                self.publish(result)
        except Exception:
            _LOGGER.exception("on_message")

    def publish(self, message: Message, **topic_args):
        """Publish a Hermes message to MQTT."""
        try:
            _LOGGER.debug("-> %s", message)
            topic = message.topic(**topic_args)
            payload = json.dumps(attr.asdict(message))
            _LOGGER.debug("Publishing %s char(s) to %s", len(payload), topic)
            self.client.publish(topic, payload)
        except Exception:
            _LOGGER.exception("on_message")

    # -------------------------------------------------------------------------

    def _check_siteId(self, json_payload: typing.Dict[str, typing.Any]) -> bool:
        if self.siteIds:
            return json_payload.get("siteId", "default") in self.siteIds

        # All sites
        return True