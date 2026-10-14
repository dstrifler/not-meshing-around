import re
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from botcore.api import Command, Message, Registration, Response
from botcore.mentions import MentionResolver, NodeIdentity
from botcore.runtime import PluginServices, Policy, Runtime, chunks
from botcore.transport import ConsoleTransport, MeshtasticTransport


ALICE = NodeIdentity(0xdeadbeef, "Alice Smith", "ALIC")
BOB = NodeIdentity(42, "Bob", "BOB")
CHARLIE = NodeIdentity(0x1234abcd, "Charlie", "ABCD")
WIRE = re.compile(r"@![0-9a-f]{8}(?![0-9a-fA-F])")


class MentionResolutionTests(unittest.TestCase):
    def setUp(self):
        self.resolver = MentionResolver((ALICE, BOB, CHARLIE, NodeIdentity(0), NodeIdentity(1),
                                         NodeIdentity(0xffffffff), NodeIdentity(12345678)))

    def test_names_short_codes_and_numbers_use_the_same_wire_token(self):
        examples = {
            "@Alice Smith @alic @3735928559 @!DEADBEEF @0xDEADBEEF @deadbeef":
                " ".join(["@!deadbeef"] * 6),
            "Hello (@Bob), @42! @0x2A and @!0000002A.":
                "Hello (@!0000002a), @!0000002a! @!0000002a and @!0000002a.",
            "@ABCD @Charlie @305441741": "@!1234abcd @!1234abcd @!1234abcd",
            "@0 @1 @4294967295": "@!00000000 @!00000001 @!ffffffff",
            "@12345678": "@!00bc614e",  # Bare digit-only numbers are decimal.
        }
        for source, expected in examples.items():
            with self.subTest(source=source):
                result = self.resolver.normalize(source)
                self.assertEqual(result, expected)
                self.assertEqual(self.resolver.normalize(result), result)

    def test_unknown_and_malformed_references_and_other_text_are_preserved(self):
        examples = ("@unknown @Alice Smithfield @ALIC_extra @ALIC.example",
                    "alice@Bob.com https://example/@BOB mailto:alice@ALIC",
                    "@4294967296 @99999999999999999999 @-1 @0x100000000",
                    "@!abc @!deadbeef00 @!123456789 @0xZZZZ @0x1234no",
                    "@00:11:22:33:44:55 @192.168.1.2 @ @[]", "normal text 🌍")
        for source in examples:
            with self.subTest(source=source):
                self.assertEqual(self.resolver.normalize(source), source)

    def test_duplicate_names_and_conflicting_numeric_short_codes_are_not_guessed(self):
        resolver = MentionResolver((NodeIdentity(1, "Shared", "SAME"),
                                    NodeIdentity(2, "shared", "same"),
                                    NodeIdentity(99, "Ninety Nine", "42"),
                                    NodeIdentity(3, "deadbeef", "THRE"), BOB, ALICE))
        self.assertEqual(resolver.normalize("@Shared @SAME @42 @deadbeef"),
                         "@Shared @SAME @42 @deadbeef")
        self.assertEqual(resolver.normalize("@!00000063 @0x63 @!deadbeef"),
                         "@!00000063 @!00000063 @!deadbeef")

    def test_unknown_nodes_are_preserved_even_when_the_numeric_identity_is_valid(self):
        source = "@Hidden Node @HIDE @123 @0xABCDEF12 @abcdef12 @!ABCDEF12 @!abcdef12"
        self.assertEqual(self.resolver.normalize(source), source)
        self.assertEqual(MentionResolver().normalize(source), source)
        # Only identities present in the database contribute to ambiguity.
        resolver = MentionResolver((NodeIdentity(99, "Hidden Number", "123"),))
        self.assertEqual(resolver.normalize("@123 @99 @!00000063"),
                         "@!00000063 @!00000063 @!00000063")

    def test_longest_known_name_wins_without_falling_back_from_an_ambiguous_name(self):
        resolver = MentionResolver((NodeIdentity(1, "Base"), NodeIdentity(2, "Base Station")))
        self.assertEqual(resolver.normalize("@Base Station and @Base."), "@!00000002 and @!00000001.")
        resolver = MentionResolver((NodeIdentity(1, "Base"), NodeIdentity(2, "Base Station"),
                                    NodeIdentity(3, "BASE STATION")))
        self.assertEqual(resolver.normalize("@Base Station and @Base"), "@Base Station and @!00000001")

    def test_unicode_casefolding_and_literal_name_characters(self):
        resolver = MentionResolver((NodeIdentity(1, "Straße", "ß"), NodeIdentity(2, "A[1].*", "ÄÖ"),
                                    NodeIdentity(3, "Jose")))
        self.assertEqual(resolver.normalize("@STRASSE @SS @A[1].* @äö"),
                         "@!00000001 @!00000001 @!00000002 @!00000002")
        self.assertEqual(resolver.normalize("@Jose\u0301"), "@Jose\u0301")

    def test_name_limits_count_utf8_bytes_and_allow_shorter_short_names(self):
        ascii_name, unicode_name = "A" * 39, "漢" * 13
        resolver = MentionResolver((NodeIdentity(1, ascii_name, "Bo"),
                                    NodeIdentity(2, unicode_name, "🌍"),
                                    NodeIdentity(3, "Compact name" + "x" * 12, "ABCD")))
        self.assertEqual(resolver.normalize(f"@{ascii_name} @{unicode_name} @Bo @🌍 @ABCD"),
                         "@!00000001 @!00000002 @!00000001 @!00000002 @!00000003")
        # A 24-byte compact database name remains an exact match too.
        self.assertEqual(resolver.normalize("@Compact name" + "x" * 12), "@!00000003")
        for source in ("@" + ascii_name + "A", "@" + unicode_name + "漢", "@ABCDx"):
            self.assertEqual(resolver.normalize(source), source)

    def test_oversized_database_names_are_ignored_without_truncating_or_losing_ids(self):
        resolver = MentionResolver((NodeIdentity(1, "A" * 40, "ABCDE"),
                                    NodeIdentity(2, "漢" * 14, "🌍x"),
                                    NodeIdentity(3, "Valid", "İİ")))
        for source in ("@" + "A" * 40, "@ABCDE", "@" + "漢" * 14, "@🌍x", "@i\u0307i\u0307"):
            self.assertEqual(resolver.normalize(source), source)
        self.assertEqual(resolver.normalize("@1 @2 @Valid @İİ"),
                         "@!00000001 @!00000002 @!00000003 @!00000003")

    def test_numeric_limits_reject_longer_fields_without_matching_a_prefix(self):
        resolver = MentionResolver((BOB, ALICE))
        for source in ("@000000000042", "@4294967296", "@0x0deadbeef", "@!0deadbeef",
                       "@deadbeef00", "@0xdeadbeefZ", "@" + "4" * 10000):
            self.assertEqual(resolver.normalize(source), source)


class MentionFormattingTests(unittest.TestCase):
    def assert_packets(self, text, limit, mentions):
        parts = chunks(text, limit)
        self.assertGreater(len(parts), 1)
        restored = []
        for index, part in enumerate(parts, 1):
            prefix = f"[{index}/{len(parts)}] "
            self.assertTrue(part.startswith(prefix))
            self.assertLessEqual(len(part.encode("utf-8")), limit)
            restored.append(part[len(prefix):])
        self.assertEqual("".join(restored), text)
        self.assertEqual([token for part in parts for token in WIRE.findall(part)], mentions)

    def test_mentions_remain_whole_even_without_surrounding_whitespace(self):
        text = "x" * 19 + "@!deadbeef" + "y" * 21 + "@!0000002a" + "🌍" * 12
        self.assert_packets(text, 24, ["@!deadbeef", "@!0000002a"])

    def test_packet_count_growth_and_utf8_do_not_split_mentions(self):
        text = "🌍@!deadbeef " * 100
        self.assertGreater(len(chunks(text, 20)), 99)
        self.assert_packets(text, 20, ["@!deadbeef"] * 100)

    def test_small_budget_fails_instead_of_splitting_a_mention(self):
        self.assertEqual(chunks("@!deadbeef", 10), ["@!deadbeef"])
        with self.assertRaisesRegex(ValueError, "split tags"):
            chunks("@!deadbeef extra", 15)
        # A malformed longer ID must not become a valid mention in one packet.
        with self.assertRaisesRegex(ValueError, "split tags"):
            chunks("prefix @!deadbeef00 suffix", 16)


class IdentitySnapshotTests(unittest.TestCase):
    def test_radio_snapshot_normalizes_sdk_fields_and_tracks_live_changes(self):
        radio = MeshtasticTransport({"type": "tcp", "hostname": "test"})
        nodes = {
            "!deadbeef": {"num": 0xdeadbeef, "user": {"longName": "Alice Smith", "shortName": "ALIC"}},
            "!0000002a": {"user": {"longName": "Bob", "shortName": "BOB"}},
            "!00000001": {"num": 2, "user": {"longName": "Wrong identity"}},
            "!+1234567": {"num": False, "user": {"longName": "Bad key"}},
            "invalid": None,
        }
        radio.interface = SimpleNamespace(nodes=nodes)
        self.assertEqual(radio.node_snapshot(), (ALICE, BOB))
        before = radio.node_snapshot()
        nodes["!deadbeef"]["user"]["shortName"] = "NEW"
        self.assertEqual(before[0].short_name, "ALIC")
        self.assertEqual(radio.node_snapshot()[0].short_name, "NEW")
        radio.interface = None
        self.assertEqual(radio.node_snapshot(), ())
        self.assertEqual(ConsoleTransport().node_snapshot(), ())
        with self.assertRaises(ValueError):
            radio.node_snapshot("secondary")


class MentionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.nodes = [ALICE, BOB, CHARLIE]
        self.sent, self.seen = [], []
        self.snapshots = 0

        def snapshot(interface):
            self.assertEqual(interface, "primary")
            self.snapshots += 1
            return tuple(self.nodes)

        async def send(*args):
            self.sent.append(args)

        self.runtime = Runtime(SimpleNamespace(send=send, node_snapshot=snapshot), ":memory:", send_delay=0)

        async def handler(context):
            self.seen.append(context)
            return Response(context.arguments)

        self.handler = handler
        await self.runtime.activate("echo", Registration(commands=(Command("echo", handler),)))

    async def asyncTearDown(self):
        await self.runtime.close()

    async def test_inbound_input_is_normalized_but_original_text_and_audience_survive(self):
        raw = "  ECHO Hi @ALIC and @42!  "
        original = Message(raw, 321, 2, direct=True, snr=0, rssi=-70)
        await self.runtime.dispatch(original)
        context = self.seen[0]
        self.assertEqual(context.arguments, "Hi @!deadbeef and @!0000002a!")
        self.assertEqual(context.message.text, "  ECHO Hi @!deadbeef and @!0000002a!  ")
        self.assertEqual(context.message.original_text, raw)
        self.assertEqual((context.message.sender, context.message.channel, context.message.snr), (321, 2, 0))
        self.assertEqual(original.text, raw)
        self.assertIsNone(original.original_text)
        self.assertEqual(self.sent, [(context.arguments, 321, 2, "primary")])
        self.assertEqual(self.snapshots, 2)  # One snapshot for input, one for output.

    async def test_command_alias_spelling_is_not_rewritten_before_routing(self):
        self.nodes.append(NodeIdentity(7, "status"))
        await self.runtime.activate("status", Registration(commands=(Command("@status", self.handler),)))
        await self.runtime.dispatch(Message("@status @42", 321, direct=True))
        self.assertEqual(self.seen[0].command, "@status")
        self.assertEqual(self.seen[0].arguments, "@!0000002a")
        self.assertEqual(self.seen[0].message.original_text, "@status @42")

    async def test_delayed_replies_and_unsolicited_messages_share_normalization_and_policy(self):
        services = PluginServices(self.runtime, "echo")
        origin = Message("echo", 321, 3, direct=True)
        await services.reply(Response("Hi @Bob"), origin)
        self.assertEqual(self.sent[-1], ("Hi @!0000002a", 321, 3, "primary"))
        with self.assertRaises(PermissionError):
            await services.send(Response("Hello @ALIC", "channel", 2))
        self.runtime.policy = Policy(broadcast_plugins=frozenset({"echo"}))
        await services.send(Response("Hello @ALIC", "channel", 2))
        self.assertEqual(self.sent[-1], ("Hello @!deadbeef", None, 2, "primary"))

    async def test_normalization_precedes_byte_splitting_and_bad_budgets_send_nothing(self):
        self.runtime.byte_limit = 20
        await self.runtime.deliver("echo", Response("@ALIC " * 20), Message("echo", 321, direct=True))
        self.assertEqual(sum(len(WIRE.findall(packet[0])) for packet in self.sent), 20)
        self.assertTrue(all(len(packet[0].encode()) <= 20 for packet in self.sent))
        self.sent.clear()
        self.runtime.byte_limit = 15
        with self.assertRaises(ValueError):
            await self.runtime.deliver("echo", Response("@ALIC extra"), Message("echo", 321, direct=True))
        self.assertEqual(self.sent, [])

    async def test_node_renames_are_used_on_the_next_delivery(self):
        await self.runtime.deliver("echo", Response("@ALIC"), Message("echo", 321, direct=True))
        self.nodes[0] = NodeIdentity(ALICE.number, "Alice Renamed", "NEW")
        await self.runtime.deliver("echo", Response("@ALIC @NEW"), Message("echo", 321, direct=True))
        self.assertEqual([packet[0] for packet in self.sent], ["@!deadbeef", "@ALIC @!deadbeef"])

    async def test_unknown_nodes_pass_through_incoming_and_outgoing_messages(self):
        tags = "@Hidden Node @HIDE @123 @0xABCDEF12 @abcdef12 @!ABCDEF12"
        await self.runtime.dispatch(Message("echo " + tags, 321, direct=True))
        self.assertEqual(self.seen[-1].arguments, tags)
        self.assertEqual(self.sent[-1][0], tags)

    async def test_transport_without_node_data_preserves_all_references(self):
        self.runtime.transport = SimpleNamespace(send=self.runtime.transport.send)
        tags = "@42 @0xdeadbeef @ALIC @!DEADBEEF"
        await self.runtime.dispatch(Message("echo " + tags, 321, direct=True))
        self.assertEqual(self.sent[-1][0], tags)

    async def test_radio_ingress_and_sdk_send_use_wire_mentions_without_changing_the_recipient(self):
        radio = MeshtasticTransport({"type": "tcp", "hostname": "test"})
        interface = SimpleNamespace(myInfo=SimpleNamespace(my_node_num=99), close=Mock(), sendText=Mock(), nodes={
            "!deadbeef": {"num": ALICE.number, "user": {"longName": ALICE.long_name, "shortName": ALICE.short_name}},
            "!0000002a": {"num": BOB.number, "user": {"longName": BOB.long_name, "shortName": BOB.short_name}},
            "!00000063": {"num": 99, "user": {"shortName": "BOT"}},
        })
        radio.interface, radio.status = interface, "ready"
        radio.runtime = self.runtime
        self.runtime.transport = radio
        try:
            await radio._handle({"from": 321, "to": 99, "channel": 3,
                "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": b"echo @ALIC @42"}})
            self.assertEqual(self.seen[-1].message.original_text, "echo @ALIC @42")
            interface.sendText.assert_called_once_with("@!deadbeef @!0000002a", channelIndex=3, destinationId=321)
        finally:
            await radio.close()
