import asyncio
import binascii
import collections.abc
import functools
import logging
import operator
import random

from . import jid, errors
from .utils import etree, namespaces, split_tag

logger = logging.getLogger(__name__)

def iterator_has_more_than(iterable, n_elements):
    try:
        return len(iterable) > n_elements
    except:
        pass

    iterator = iter(iterable)
    for i in range(n_elements+1):
        try:
            next(iterator)
        except StopIteration:
            return False
    return True

class xml_attribute:
    def __init__(self, key):
        self._key = key

    def __get__(self, instance, type_):
        if instance is None:
            return self

        return instance.get(self._key)

    def __set__(self, instance, value):
        if value is None:
            return self.__delete__(instance)

        instance.set(self._key, value)

    def __delete__(self, instance):
        try:
            del instance.attrib[self._key]
        except KeyError:
            pass

class xml_jid_attribute(xml_attribute):
    def __get__(self, instance, type_):
        if instance is None:
            return self

        value = super().__get__(instance, type_)
        if value is None:
            return None
        return jid.JID.fromstr(value)

    def __set__(self, instance, value):
        if value is None:
            return super().__set__(instance, value)

        return super().__set__(instance, str(value))

class xml_enum_attribute(xml_attribute):
    def __init__(self, key, options):
        super().__init__(key)
        self._options = options

    def __set__(self, instance, value):
        if value is None:
            return super().__set__(instance, value)

        if value not in self._options:
            _, localname = split_tag(instance.tag)
            raise ValueError("Invalid value for {localname}@{key}: {}".format(
                value,
                localname=localname,
                key=self._key))

        return super().__set__(instance, str(value))

class StanzaMeta(type):
    pass

class StanzaElementBase(etree.ElementBase):
    def __init__(self, *args, nsmap=None, **kwargs):
        if nsmap is None:
            nsmap = {}
        else:
            nsmap = dict(nsmap)
        ns, _ = split_tag(self.TAG)
        if ns is not None:
            nsmap[None] = ns
        super().__init__(*args, nsmap=nsmap, **kwargs)

class Stanza(StanzaElementBase):
    def validate(self):
        pass

    def autoset_id(self):
        if self.id is None:
            idstr = binascii.b2a_base64(random.getrandbits(
                120
            ).to_bytes(
                120//8, 'little'
            )).decode("ascii").strip()
            self.set("id", idstr)

    id = xml_attribute("id")
    from_ = xml_jid_attribute("from")
    to = xml_jid_attribute("to")

class Message(Stanza):
    TAG = "{jabber:client}message"

    type = xml_attribute("type")

    @property
    def body(self):
        body = self.find("{jabber:client}body")
        if body is None:
            body = etree.SubElement(self, "{jabber:client}body")
        return body

    @body.deleter
    def body(self):
        body = self.body
        self.remove(body)

    def make_reply(self, type=None):
        obj = Message()
        # hack to associate the IQ with the correct parser
        self.append(obj)
        self.remove(obj)
        if self.to:
            obj.from_ = self.to
        if self.from_:
            obj.to = self.from_
        obj.id = self.id
        obj.type = type or self.type
        return obj

class Presence(Stanza):
    TAG = "{jabber:client}presence"
    _VALID_TYPES = {"unavailable",
                    "subscribe", "subscribed",
                    "unsubscribe", "unsubscribed"}

    type = xml_enum_attribute("type", _VALID_TYPES)

    def make_reply(self, type=None):
        obj = Presence()
        # hack to associate the IQ with the correct parser
        self.append(obj)
        self.remove(obj)
        if self.to:
            obj.from_ = self.to
        if self.from_:
            obj.to = self.from_
        obj.id = self.id
        obj.type = type or self.type
        return obj

class IQ(Stanza):
    TAG = "{jabber:client}iq"
    _VALID_TYPES = {"set", "get", "error", "result"}

    def validate(self):
        super().validate()
        if self.type not in self._VALID_TYPES:
            raise ValueError("Incorrect iq@type: {}".format(self.type))
        if self.type in {"set", "get"}:
            if self.data is None:
                raise ValueError("iq with type 'set' or 'get' must have "
                                 "exactly one non-error child")
        elif self.type == "result":
            if len(self) > 1 or self.error is not None:
                raise ValueError("iq with type 'result' must have zero or"
                                 " one non-error child")
        elif self.type == "error":
            if self.error is None:
                raise ValueError("iq with type 'error' must have one error "
                                 "child")

    type = xml_enum_attribute("type", _VALID_TYPES)

    @property
    def data(self):
        try:
            return next(iter(
                node
                for node in self
                if node.tag != Error.TAG))
        except StopIteration:
            return None

    @data.setter
    def data(self, value):
        del self.data
        self.append(value)

    @data.deleter
    def data(self):
        data = self.data
        if data is not None:
            self.remove(data)

    @property
    def error(self):
        return self.find(Error.TAG)

    @error.setter
    def error(self, value):
        if self.type != "error":
            raise ValueError("Error cannot be attached to an IQ of type "
                             "{}".format(self.type))

        del self.error
        self.append(value)

    @error.deleter
    def error(self):
        error = self.error
        if error is not None:
            self.remove(error)

    def make_reply(self, error=False):
        """
        Return a new :class:`IQ` instance. The instance has :attr:`from_` and
        :attr:`to` swapped, but share the same :attr:`id`.

        The type is set to ``"result"`` if *error* is :data:`False`, else it is
        set to ``"error"`` and :attr:`data` is initialized with a new
        :class:`Error` instance.
        """

        if self.type not in {"set", "get"}:
            raise ValueError("Cannot construct reply to iq@type={!r}".format(
                type))

        obj = IQ()
        # hack to associate the IQ with the correct parser
        self.append(obj)
        self.remove(obj)
        obj.from_ = self.to
        obj.to = self.from_
        obj.id = self.id
        if error:
            obj.type = "error"
            obj.error = Error()
        else:
            obj.type = "result"

        return obj


class Error(etree.ElementBase):
    TAG = "{jabber:client}error"
    _VALID_TYPES = {"auth", "cancel", "continue", "modify", "wait"}
    _TEXT_ELEMENT = "{{{}}}text".format(namespaces.stanzas)
    _VALID_CONDITIONS = frozenset((
        "bad-request",
        "conflict",
        "feature-not-implemented",
        "forbidden",
        "gone",
        "internal-server-error",
        "item-not-found",
        "jid-malformed",
        "not-acceptable",
        "not-allowed",
        "not-authorized",
        "policy-violation",
        "recipient-unavailable",
        "redirect",
        "registration-required",
        "remote-server-not-found",
        "remote-server-timeout",
        "resource-constraint",
        "service-unavailable",
        "subscription-required",
        "undefined-condition",
        "unexpected-request",
    ))

    _VALID_CONDITION_ELEMENTS = frozenset(
        "{{{}}}{}".format(namespaces.stanzas, condition)
        for condition in _VALID_CONDITIONS
    )
    _NON_APPDEF_ELEMENTS = _VALID_CONDITION_ELEMENTS | {_TEXT_ELEMENT}
    _CONDITION_FILTER = "{{{}}}*".format(namespaces.stanzas)

    type = xml_enum_attribute("type", _VALID_TYPES)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.condition = "undefined-condition"

    def _iter_condition_elements(self):
        return (el for el in self.iterchildren(self._CONDITION_FILTER)
                if el.tag in self._VALID_CONDITION_ELEMENTS)

    def _iter_text_elements(self):
        return self.iterchildren(self._TEXT_ELEMENT)

    def _iter_appdef_elements(self):
        return (el for el in self.iterchildren()
                if el.tag not in self._NON_APPDEF_ELEMENTS)

    def validate(self):
        if self.condition is None:
            self.condition = "undefined-condition"

        if iterator_has_more_than(self._iter_condition_elements(), 1):
            raise ValueError("Malformed error element "
                             "(multiple conditions)")

        if iterator_has_more_than(self._iter_text_elements(), 1):
            raise ValueError("Malformed error element "
                             "(multiple texts)")

        if iterator_has_more_than(self._iter_appdef_elements(), 1):
            raise ValueError("Malformed error element "
                             "(multiple application defined conditions)")


    @property
    def condition(self):
        try:
            el = next(self._iter_condition_elements())
        except StopIteration:
            return None
        else:
            return split_tag(el.tag)[1]

    @condition.setter
    def condition(self, value):
        existing = self.condition
        if value not in self._VALID_CONDITIONS:
            raise ValueError("{!r} is not a valid error condition".format(
                value))

        if existing == value:
            return

        value = "{{{}}}{}".format(namespaces.stanzas, value)
        if existing is not None:
            self.find("{{{}}}{}".format(
                namespaces.stanzas,
                existing)).tag = value
        else:
            self.insert(0, self.makeelement(value))

    def _find_text_element(self):
        el = self.find(self._TEXT_ELEMENT)
        if el is None or el.text is None:
            return None
        return el

    def _find_or_make_text_element(self):
        el = self._find_text_element()
        if el is None:
            el = self.makeelement(self._TEXT_ELEMENT)
            self.insert(1, el)
        return el

    @property
    def text(self):
        el = self._find_text_element()
        if el is None:
            return None
        return el.text

    @text.setter
    def text(self, value):
        el = self._find_or_make_text_element()
        el.text = value

    @text.deleter
    def text(self):
        el = self.find(self._TEXT_ELEMENT)
        if el is not None:
            self.remove(el)

    @property
    def text_lang(self):
        el = self._find_text_element()
        if el is None:
            return None
        return el.get("{http://www.w3.org/XML/1998/namespace}lang")

    @property
    def text_lang(self, value):
        el = self._find_or_make_text_element()
        el.set("{http://www.w3.org/XML/1998/namespace}lang", value)

    @property
    def application_defined_condition(self):
        try:
            return next(iter(self._iter_appdef_elements()))
        except StopIteration:
            return None

    @application_defined_condition.setter
    def application_defined_condition(self, value):
        existing = self.application_defined_condition
        if existing is not None:
            self.remove(existing)
        self.append(value)

    @application_defined_condition.deleter
    def application_defined_condition(self):
        existing = self.application_defined_condition
        if existing is not None:
            self.remove(existing)

    def make_exception(self):
        cls = errors.error_type_map.get(
            self.type,
            errors.XMPPError)

        return cls(
            self.condition,
            text=self.text,
            application_defined_condition=self.application_defined_condition)


class QueueState:
    ACTIVE = 0
    UNACKED = 1
    SENT_WITHOUT_ACK = 2
    ABORTED = 3
    ACKED = 4

class StanzaToken:
    """
    Token to represent a stanza which is inside or has gone through the queues
    of a :class:`Client`.

    The constructor is considered an implementation detail. It is called by the
    :class:`Client` object. Instances of :class:`StanzaToken` can be obtained by
    the respective methods of :class:`Client`. **TODO:** which methods

    A stanza can have one out of five states:

    * :attr:`~QueueState.ACTIVE`: The stanza is queued for sending and has not
      been sent yet. New stanzas are in this state.
    * :attr:`~QueueState.UNACKED`: The stanza has been handed over to the
      transport layer, but no confirmation from the remote side has been
      received yet. Nevertheless, Stream Management is available.
    * :attr:`~QueueState.SENT_WITHOUT_ACK`: The stanza has been handed over to
      the transport layer, but stream management is not
      available. Alternatively, a stanza which was previously ``UNACKED`` can
      end up in this state if the connection gets interrupted and stream
      management session resumption fails. This is a final state, as the stanza
      is removed from all queues when it enters this state.
    * :attr:`~QueueState.ABORTED`: The stanza has been aborted by the
      application. This is a final state, as the stanza is removed from all
      queues when it enters this state.
    * :attr:`~QueueState.ACKED`: A stream management confirmation of the
      reception of this stanza has been received. This is a final state, as the
      stanza is removed from all queues when it enters this state.
    """

    def __init__(self, stanza,
                 sent_callback=None,
                 ack_callback=None,
                 response_future=None,
                 abort_impl=None,
                 resend_impl=None):
        self._abort_impl = abort_impl
        self._resend_impl = resend_impl
        self._stanza = stanza
        self._state = QueueState.ACTIVE
        self._last_sent = None
        self.ack_callback = ack_callback
        self.sent_callback = sent_callback
        self.response_future = response_future

    def abort(self):
        """
        Abort sending a stanza. Depending on the current state of the stanza,
        this has different meanings:

        * If the stanza is in *active* state, it won’t be send and silently
          dropped from the *active* queue.
        * If the stanza is in *unacked* state, it won’t be resent on session
          resumption (but it may nevertheless have been received by the remote
          party already). The behaviour which encurs when an *unacked, aborted*
          stanza is acked by the remote side is still to be specified.

        In any other state, calling this method has no effect.
        """
        if not hasattr(self._abort_impl, "__call__"):
            raise NotImplementedError("Stanza abortion is not supported by "
                                      "the object which supplied the stanza "
                                      "token")

        self._state = self._abort_impl(self)

    def resend(self):
        """
        For a stanza *sent without ack* or *aborted*, re-submit it to the active
        queue. If the stanza is already in the *active* queue or if it is
        *unacked*, do nothing.
        """
        if not hasattr(self._resend_impl, "__call__"):
           raise NotImplementedError("Stanza resending is not supported by "
                                     "the object which supplied the stanza "
                                     "token")

        self._state = self._resend_impl(self)

    @property
    def state(self):
        return self._state

    @property
    def stanza(self):
        return self._stanza

    @property
    def last_sent(self):
        """
        Return the timestamp at which this stanza was last attempted to be sent
        over the stream.
        """
        return self._last_sent


class ChildrenSetProxy(collections.abc.MutableSet):
    def __init__(self, node, selector):
        self._node = node
        self._selector = selector

    def _iter_nodes(self):
        return self._node.iterchildren(self._selector)

    def __iter__(self):
        return self._iter_nodes()

    def __len__(self):
        return sum(1 for node in self._iter_nodes())

    def __contains__(self, other_node):
        return other_node.tag == self._selector and other_node in self._node

    def add(self, node):
        if node in self:
            return
        self._node.append(node)

    def remove(self, node):
        try:
            self._node.remove(node)
        except ValueError:
            raise KeyError(node) from None

    def discard(self, node):
        try:
            self.remove(node)
        except KeyError:
            pass

    def clear(self):
        for node in list(self._iter_nodes()):
            self._node.remove(node)

class TransformedChildrenSetProxy(ChildrenSetProxy):
    def __init__(self, node, selector,
                 constructor_func,
                 map_func):
        super().__init__(node, selector)
        self._constructor_func = constructor_func
        self._map_func = map_func

    def __iter__(self):
        return map(self._map_func, self._iter_nodes())

    def __contains__(self, key):
        return any(self._test_func(node, key)
                   for node in self._iter_nodes())

    def add(self, key):
        if key in self:
            return

        self._constructor_func(
            self._node,
            key)

    def remove(self, key):
        for node in self._iter_nodes():
            if self._map_func(node) == key:
                self.remove(node)
                return
        raise KeyError(key)

    def discard(self, key):
        try:
            self.remove(key)
        except KeyError:
            pass

    def clear(self):
        for node in list(self._iter_nodes()):
            self._node.remove(node)