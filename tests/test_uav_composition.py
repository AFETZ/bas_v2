"""Composition/transport and onboard failsafe checks; no flight or hardware claims."""
import copy
import json
import os
import select
import time
from collections import deque
from pathlib import Path
import shutil
import signal
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

from network.coordination.policy import ProtocolError, AdmissionGate, Authority, UARTAdmission, make_command, decode, encode
from network.scripts.serial_transport import SerialBaudBudget, Encoder, Reassembler
from network.coordination.service import (AUTOPILOT_MODE_MAX_AGE_NS, Network, UAVService,
                                          build_parser, configured_uav_ids)
from scripts.product import prepare_urban_gazebo as generator
from scripts.product.urban_mission import UrbanScenario


class CompositionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scene = json.loads((generator.ROOT / 'network/config/urban_low.json').read_text())
        target = self.root / 'network/config'
        target.mkdir(parents=True)
        for name in ('scenario_5uav_town01_native_product.yaml', 'town01_sitl.parm'):
            shutil.copy2(generator.ROOT / 'network/config' / name, target / name)

    def scene_for(self, count):
        data = copy.deepcopy(self.scene)
        ids = {str(i) for i in range(1, count + 1)}
        data['spawns'] = {i: p for i, p in data['spawns'].items() if i in ids}
        data['routes'] = {i: p for i, p in data['routes'].items() if i in ids}
        # A new scene declares its barriers explicitly, rather than pruning unknown references at runtime.
        data['mission']['barriers'] = {}
        return data

    def write_scene(self, data):
        path = self.root / 'scene.json'
        path.write_text(json.dumps(data))
        return path

    def test_each_roster_generates_only_declared_nodes_ports_and_missions(self):
        for count in range(1, 6):
            with self.subTest(count=count):
                path = self.write_scene(self.scene_for(count))
                loaded = UrbanScenario.load(path)
                self.assertEqual(loaded.uav_ids, tuple(range(1, count + 1)))
                output = self.root / f'generated-{count}'
                with patch.object(generator, 'ROOT', self.root):
                    generator.prepare(path, output, count)
                launch = yaml.safe_load((output / 'scenario.yaml').read_text())
                names = {f'uav{i}' for i in range(1, count + 1)}
                self.assertEqual({r['name'] for r in launch['robots']}, names)
                self.assertEqual(set(launch['flight']['missions']), names)
                self.assertEqual(set(launch['flight']['takeoff_relative_altitude_m']), names)
                self.assertEqual([r['instance'] for r in launch['robots']], list(range(count)))
                for key in ('system_id', 'dds_udp_port', 'master_tcp_port', 'sitl_udp_port', 'fdm_udp_port'):
                    self.assertEqual(len({r[key] for r in launch['robots']}), count)
                for capture in launch['evidence']['screenshots']:
                    self.assertLessEqual(set(capture['required_projected_uavs']), names)
                self.assertNotIn('\\', launch['scenario']['map']['world_file'])
                self.assertEqual(json.loads((output / 'geometry-source.json').read_text())['config'], loaded.raw)

    def test_unknown_or_incomplete_rosters_are_not_silently_sliced(self):
        examples = []
        data = self.scene_for(2)
        data['routes']['3'] = [[10, 0, 12]]
        examples.append(data)
        data = self.scene_for(2)
        data['spawns']['3'] = data['spawns'].pop('2')
        data['routes']['3'] = data['routes'].pop('2')
        examples.append(data)
        data = self.scene_for(1)
        data['spawns']['01'] = data['spawns'].pop('1')
        data['routes']['01'] = data['routes'].pop('1')
        examples.append(data)
        data = self.scene_for(1)
        data['mission']['barriers'] = {'1:1': {'minimum_reached': {'2': 0}}}
        examples.append(data)
        examples.append({'spawns': {}, 'routes': {}})
        for data in examples:
            with self.subTest(data=data), self.assertRaises(ValueError):
                UrbanScenario.load(self.write_scene(data))

    def test_expected_count_mismatch_fails_before_generating(self):
        output = self.root / 'not-generated'
        with self.assertRaisesRegex(ValueError, 'roster'):
            generator.prepare(self.write_scene(self.scene_for(2)), output, 1)
        self.assertFalse(output.exists())

    def test_service_parser_and_count_reject_invalid_counts(self):
        for value in (0, 6, True, 2.0, '2'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                configured_uav_ids(SimpleNamespace(uav_count=value))
        args = build_parser().parse_args(['gcs', '--run-id', 'test', '--event-log', 'unused.jsonl', '--uav-count', '2'])
        self.assertEqual(configured_uav_ids(args), (1, 2))
        self.assertEqual(configured_uav_ids(SimpleNamespace()), (1, 2, 3, 4, 5))

    def test_real_udp_probe_exchange_uses_each_configured_roster(self):
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        self.addCleanup(lambda: [signal.signal(sig, value) for sig, value in previous.items()])
        for count in range(1, 6):
            with self.subTest(count=count), patch('network.coordination.service.endpoint_ip',
                                                 side_effect=lambda i: f'127.0.0.{i + 1}'):
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
                    reservation.bind(('127.0.0.1', 0))
                    port = reservation.getsockname()[1]
                args = SimpleNamespace(uav_count=count, port=port, heartbeat_s=.2,
                                       run_id='composition', radio_watchdog_file=None, watchdog_s=.3)
                nodes = []
                try:
                    nodes = [Network(args, i, lambda *a, **kw: None) for i in range(count + 1)]
                    for node in nodes:
                        node.udp.settimeout(1)
                    nodes[0].periodic(1_000_000_000)
                    for node in nodes[1:]:
                        self.assertIsNone(node.receive())  # Actual UDP probe causes an actual UDP response.
                    received = {nodes[0].receive()[1] for _ in range(count)}
                    self.assertEqual(received, set(range(1, count + 1)))
                    self.assertEqual(set(nodes[0].peer_echo_ns), received)
                    with self.assertRaisesRegex(ProtocolError, 'roster'):
                        nodes[0].send('probe', {}, count + 1)
                finally:
                    for node in nodes:
                        node.close()


class LandingFailsafeTests(unittest.TestCase):
    def fixture(self, mode=None, age_ns=0, armed=True):
        node = UAVService.__new__(UAVService)
        node.index = 1
        node.latest_autopilot_mode = None if mode is None else dict(
            mode=mode, custom_mode=9 if mode == 'LAND' else 4, base_mode=128 if armed else 0,
            armed=armed, received_host_monotonic_ns=10_000_000_000 - age_ns)
        node.hold_suppressed_for_land = False
        node.hold_pending, node.last_hold_ns, node.hold_confirmed = None, 0, False
        node.mavutil = SimpleNamespace(mode_mapping_acm={4: 'GUIDED', 9: 'LAND', 17: 'BRAKE'},
                                       mavlink=SimpleNamespace(MAV_MODE_FLAG_CUSTOM_MODE_ENABLED=1,
                                                               MAV_MODE_FLAG_SAFETY_ARMED=128))
        node.mav = Mock(seq=0)
        node.mav.set_mode_encode.return_value.pack.return_value = b'BRAKE'
        node.events = []
        node.emit = lambda event, **fields: node.events.append((event, fields))
        node.admission = SimpleNamespace(queue=[])
        node.write_uart = Mock(return_value=5)
        return node

    def test_fresh_land_survives_lease_expiry_without_claiming_hold(self):
        node = self.fixture('LAND')
        with patch('network.coordination.service.time.monotonic_ns', return_value=10_000_000_000):
            node.request_hold()
            node.request_hold()
        self.assertIsNone(node.hold_pending)
        node.mav.set_mode_encode.assert_not_called()
        self.assertEqual([name for name, _ in node.events], ['hold_suppressed'])
        self.assertEqual(node.events[0][1]['reason'], 'observed_autonomous_landing')
        self.assertFalse(node.hold_confirmed)

    def test_land_observed_after_brake_queued_cancels_the_queued_override(self):
        node = self.fixture('LAND')
        node.hold_pending = b'BRAKE'
        with patch('network.coordination.service.time.monotonic_ns', return_value=10_000_000_000):
            node.write_hold()
        node.write_uart.assert_not_called()
        self.assertIsNone(node.hold_pending)

    def test_guided_unknown_stale_and_future_mode_keep_existing_hold(self):
        for mode, age in [('GUIDED', 0), (None, 0), ('LAND', AUTOPILOT_MODE_MAX_AGE_NS + 1), ('LAND', -1)]:
            with self.subTest(mode=mode, age=age):
                node = self.fixture(mode, age)
                with patch('network.coordination.service.time.monotonic_ns', return_value=10_000_000_000):
                    node.request_hold()
                    node.write_hold()
                node.write_uart.assert_called_once_with(b'BRAKE')
                self.assertEqual(node.events[-1][0], 'hold_uart_written')

    def test_actual_own_heartbeat_records_mode_armed_and_receipt_time(self):
        from pymavlink import mavutil
        node = self.fixture()
        node.mavutil = mavutil
        node.uart_parser = mavutil.mavlink.MAVLink(None); node.uart_parser.robust_parsing = True
        raw = b''
        for i in (1, 255):
            tx = mavutil.mavlink.MAVLink(None, srcSystem=i, srcComponent=1)
            raw += tx.heartbeat_encode(mavutil.mavlink.MAV_TYPE_QUADROTOR,
                mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA, 128, 9, 4).pack(tx, force_mavlink1=False)
        node.gate = SimpleNamespace(active_grant=lambda: None)
        with patch('network.coordination.service.time.monotonic_ns', return_value=10_000_000_000):
            node.telemetry(raw)
        self.assertTrue(node.latest_autopilot_mode['armed'])
        self.assertEqual(node.latest_autopilot_mode['received_host_monotonic_ns'], 10_000_000_000)
        self.assertEqual([event for event, _ in node.events].count('autopilot_mode_observed'), 1)


class ControlUartPacingTests(unittest.TestCase):
    """Production policy/writer and real OS pipes; controlled clocks, no physical UART or RF proof."""
    def setUp(self):
        from pymavlink import mavutil
        self.now = 10_000_000_000
        self.addCleanup(patch.stopall)
        patch('network.coordination.service.time.monotonic_ns', lambda: self.now).start()
        node = self.node = UAVService.__new__(UAVService)
        node.index, node.running = 1, True
        node.args = SimpleNamespace(run_id='pacing-test', command_max_age_ms=250, telemetry_max_age_ms=250)
        node.events = []
        node.emit = lambda event, **fields: node.events.append((event, fields))
        node.guard_radio = lambda: None
        node.uart_timing = dict(owner='software_pty', bits_per_byte=10, physical_validation='not_performed')
        node.uart_up_budget, node.uart_down_budget = SerialBaudBudget(115200, 10), SerialBaudBudget(115200, 10)
        node.uart_registered, node.selector, node.udp = True, Mock(), Mock()
        node.telemetry_pending = None
        node.uart_stream_provenance = deque()
        node.uart_input_bytes = node.uart_output_bytes = node.uart_telemetry_age_drops = 0
        node.uart_io_blocked, node.uart_backpressure_events = False, 0
        node.uart_continuity_valid = True
        node.hold_pending, node.hold_offset, node.hold_created_ns = None, 0, 0
        node.last_hold_ns, node.hold_confirmed = 0, True
        node.latest_autopilot_mode, node.hold_suppressed_for_land = None, False
        node.mavutil = mavutil
        node.mav = mavutil.mavlink.MAVLink(None, srcSystem=255, srcComponent=190)
        node.uart_parser = mavutil.mavlink.MAVLink(None); node.uart_parser.robust_parsing = True
        node.encoder = Encoder(channel='control', uav_id=1, direction='uart_to_gcs')
        node.uav_ids = (1,)
        self.authority = Authority(node.args.run_id)
        self.grant = self.authority.issue(1, self.now, 2_000_000_000)
        node.gate = AdmissionGate(node.args.run_id, 1, now_ns=lambda:self.now, hold=node.request_hold, emit=node.emit)
        node.gate.receive_grant(self.grant, sender_id=0)
        node.admission = UARTAdmission(node.gate, node.write_uart, emit=node.admission_event)
        self.read_fd, self.write_fd = os.pipe()
        self.addCleanup(self.close_fd, self.read_fd); self.addCleanup(self.close_fd, self.write_fd)
        node.uart = self.write_fd

    @staticmethod
    def close_fd(fd):
        try: os.close(fd)
        except OSError: pass

    def frame(self, mode=4):
        return bytes(self.node.mav.set_mode_encode(1, 1, mode).pack(self.node.mav, force_mavlink1=False))

    def enqueue(self, sequence=1, payload=None):
        command = make_command(self.grant, destination_id=1, sequence=sequence,
                               now_ns=self.now, payload=payload or self.frame())
        self.node.command(1, command)
        return command

    def wire_time(self, count):
        return (count * 10 * 1_000_000_000 + 115199) // 115200

    def test_command_waits_for_wire_budget_then_original_policy_admits_real_pipe_bytes(self):
        command = self.enqueue()
        self.assertFalse(self.node.admission.drain_one())
        self.assertEqual(self.node.admission.queue[0][2], 0)
        self.now += self.wire_time(len(command.payload))
        self.node.drain_uart()
        self.assertEqual(os.read(self.read_fd, len(command.payload)), command.payload)
        admitted = [row for event,row in self.node.events if event=='uart_admitted']
        self.assertEqual(len(admitted), 1)
        self.assertIs(admitted[0]['command'], command)
        self.assertEqual(command.created_ns, 10_000_000_000)
        self.assertEqual(self.node.uart_down_budget.snapshot()['charged_bytes'], len(command.payload))

    def test_partial_write_and_eagain_preserve_offsets_id_deadline_and_budget(self):
        command = self.enqueue(); self.now += self.wire_time(len(command.payload))
        real_write = os.write
        writes = 0
        def write(fd, data):
            nonlocal writes
            writes += 1
            if writes == 1: return real_write(fd, data[:5])
            if writes == 2: raise BlockingIOError()
            return real_write(fd, data)
        with patch('network.coordination.service.os.write', write):
            self.assertTrue(self.node.admission.drain_one())
            first = list(self.node.admission.queue[0])
            self.assertFalse(self.node.admission.drain_one())
            self.assertEqual(list(self.node.admission.queue[0]), first)
            self.assertEqual(self.node.uart_down_budget.snapshot()['charged_bytes'], 5)
            self.assertGreater(self.node.uart_wait(self.now), 0)  # Backpressure must not become a zero-time spin.
            self.assertTrue(self.node.admission.drain_one())
        self.assertEqual(os.read(self.read_fd, len(command.payload)), command.payload)
        self.assertEqual([row['command'].command_id for event,row in self.node.events if event=='uart_admitted'], [command.command_id])

    def test_valid_large_mavlink_frame_crosses_two_baud_batches_without_early_admission(self):
        message = self.node.mav.encapsulated_data_encode(7,list(range(253)))
        payload = bytes(message.pack(self.node.mav,force_mavlink1=False))
        self.assertGreater(len(payload),192)
        command = self.enqueue(payload=payload)
        self.now += self.wire_time(192)
        self.assertTrue(self.node.admission.drain_one())
        self.assertEqual(self.node.admission.queue[0][2],192)
        self.assertFalse(any(event=='uart_admitted' for event,_ in self.node.events))
        self.assertFalse(self.node.admission.drain_one())
        self.now += self.wire_time(len(payload)-192)
        self.assertTrue(self.node.admission.drain_one())
        self.assertEqual(os.read(self.read_fd,len(payload)),payload)
        self.assertEqual(self.node.uart_down_budget.snapshot()['charged_bytes'],len(payload))
        self.assertEqual(next(row['command'] for event,row in self.node.events if event=='uart_admitted'),command)

    def test_expired_unwritten_head_cannot_fund_an_instant_fresh_command(self):
        self.enqueue(); self.now += 251_000_000
        self.assertTrue(self.node.admission.drain_one())
        self.assertEqual(self.node.uart_down_budget.snapshot()['charged_bytes'], 0)
        fresh = self.enqueue(2)
        self.assertFalse(self.node.admission.drain_one())
        self.now += self.wire_time(len(fresh.payload))
        self.assertTrue(self.node.admission.drain_one())
        self.assertEqual(os.read(self.read_fd, len(fresh.payload)), fresh.payload)

    def test_expired_partially_written_command_is_invalid_not_hold_or_success(self):
        command = self.enqueue(); self.now += self.wire_time(len(command.payload))
        real_write = os.write
        with patch('network.coordination.service.os.write', lambda fd,data: real_write(fd,data[:5])):
            self.node.admission.drain_one()
        self.now = command.created_ns + 251_000_000
        with self.assertRaisesRegex(RuntimeError,'partially written'):
            self.node.admission.drain_one()
        self.assertEqual(os.read(self.read_fd,5), command.payload[:5])
        self.assertFalse(any(event=='uart_admitted' for event,_ in self.node.events))
        self.assertEqual(next(row['bytes'] for event,row in self.node.events if event=='uart_write_invalidated'),5)
        self.node.close()
        summary = next(row for event,row in self.node.events if event=='uart_pacing_summary')
        self.assertFalse(summary['continuity_valid'])  # The policy already removed the invalidated prefix's queue entry.

    def start_partial_hold(self):
        self.node.request_hold(); frame = self.node.hold_pending
        self.now += self.wire_time(len(frame))
        real_write = os.write
        with patch('network.coordination.service.os.write', lambda fd,data: real_write(fd,data[:5])):
            self.assertTrue(self.node.write_hold())
        return frame

    def test_started_hold_finishes_before_new_land_command_without_interleaving(self):
        hold = self.start_partial_hold()
        land = self.enqueue(payload=self.frame(9))
        self.node.drain_uart()
        self.assertEqual(os.read(self.read_fd,len(hold)),hold)
        self.assertEqual(self.node.admission.queue[0][0],land)
        self.assertEqual(self.node.admission.queue[0][2],0)
        self.now += self.wire_time(len(land.payload))
        self.node.drain_uart()
        self.assertEqual(os.read(self.read_fd,len(land.payload)),land.payload)
        order = [event for event,_ in self.node.events if event in ('hold_uart_written','uart_admitted')]
        self.assertEqual(order,['hold_uart_written','uart_admitted'])

    def test_observed_land_after_hold_prefix_or_partial_hold_deadline_fails_closed(self):
        hold = self.start_partial_hold()
        self.node.latest_autopilot_mode = dict(mode='LAND', received_host_monotonic_ns=self.now)
        with self.assertRaisesRegex(RuntimeError,'observed_LAND_after_HOLD_prefix'):
            self.node.write_hold()
        self.assertEqual(os.read(self.read_fd,5),hold[:5])
        self.node.latest_autopilot_mode = None
        self.now = self.node.hold_created_ns + 251_000_000
        with self.assertRaisesRegex(RuntimeError,'HOLD_deadline_after_partial_write'):
            self.node.write_hold()
        self.assertFalse(any(event=='hold_uart_written' for event,_ in self.node.events))

    def test_stop_during_guard_cannot_start_a_uart_write_or_network_send(self):
        command = self.enqueue(); self.now += self.wire_time(len(command.payload))
        self.node.guard_radio = lambda:setattr(self.node,'running',False)
        with patch('network.coordination.service.os.write') as write:
            self.assertFalse(self.node.admission.drain_one())
            write.assert_not_called()
        self.assertFalse(self.node.send('probe',dict(run_id='pacing-test',sent_ns=self.now),0))
        self.node.udp.sendto.assert_not_called()

    def test_paced_uplink_keeps_original_producer_and_grant_across_renewal(self):
        self.node.uart = self.read_fd
        tx = self.node.mavutil.mavlink.MAVLink(None,srcSystem=1,srcComponent=1)
        frame = bytes(tx.heartbeat_encode(2,3,128,9,4).pack(tx,force_mavlink1=False))
        os.write(self.write_fd,frame)
        self.node.read_uart()
        producer = self.now
        self.assertIsNone(self.node.latest_autopilot_mode)
        self.node.drain_telemetry()
        self.assertIsNotNone(self.node.telemetry_pending)
        self.now += self.wire_time(len(frame))
        newer = self.authority.issue(1,self.now,2_000_000_000)
        self.node.gate.receive_grant(newer,sender_id=0)
        sent = []
        self.node.send = lambda kind,body,dst:sent.append((kind,body,dst))
        self.node.drain_telemetry()
        body = sent[0][1]
        self.assertEqual(body['producer_ns'],producer)
        self.assertEqual(body['grant_sequence'],self.grant.grant_sequence)
        receiver = Reassembler(channel='control',uav_id=1,direction='uart_to_gcs')
        self.assertEqual(receiver.ingest(body['payload'],self.now),[frame])
        self.assertEqual(receiver.released_sent_ns,[producer])
        self.assertEqual(self.node.latest_autopilot_mode['received_host_monotonic_ns'],producer)
        self.assertEqual(self.node.latest_autopilot_mode['uart_release_host_monotonic_ns'],self.now)
        self.assertEqual(self.node.uart_up_budget.snapshot()['charged_bytes'],len(frame))
        self.assertEqual(self.node.uart_down_budget.snapshot()['charged_bytes'],0)

    def test_fragmented_local_heartbeat_cannot_freshen_old_prefix(self):
        tx = self.node.mavutil.mavlink.MAVLink(None,srcSystem=1,srcComponent=1)
        frame = bytes(tx.heartbeat_encode(2,3,128,9,4).pack(tx,force_mavlink1=False))
        old = self.now
        self.node.send = Mock()
        self.node.telemetry(frame[:5],provenance=(old,None))
        self.now += 200_000_000
        self.node.telemetry(frame[5:],provenance=(self.now,None))
        self.assertEqual(self.node.latest_autopilot_mode['received_host_monotonic_ns'],old)
        self.node.send.assert_not_called()

    def test_expired_pending_telemetry_keeps_its_clock_and_is_not_parsed_as_fresh(self):
        self.node.uart = self.read_fd
        tx = self.node.mavutil.mavlink.MAVLink(None,srcSystem=1,srcComponent=1)
        frame = bytes(tx.heartbeat_encode(2,3,128,9,4).pack(tx,force_mavlink1=False))
        os.write(self.write_fd,frame); self.node.read_uart()
        original = self.now
        self.now += 251_000_000
        self.node.send = Mock()
        self.node.drain_telemetry()
        self.node.send.assert_not_called()
        self.assertIsNone(self.node.latest_autopilot_mode)
        self.assertIsNone(self.node.telemetry_pending)
        dropped = next(row for event,row in self.node.events if event=='uart_telemetry_pacing_drop')
        self.assertEqual(dropped['producer_ns'],original)
        self.assertEqual(self.node.uart_up_budget.snapshot()['charged_bytes'],0)

    def test_driver_owned_uart_is_not_software_paced(self):
        self.node.uart_down_budget = self.node.uart_up_budget = None
        command = self.enqueue()
        self.assertTrue(self.node.admission.drain_one())
        self.assertEqual(os.read(self.read_fd,len(command.payload)),command.payload)
        self.assertEqual(self.node.uart_output_bytes,len(command.payload))

    def test_stop_discards_suffix_and_exports_actual_prefix_counts_without_more_io(self):
        frame = self.start_partial_hold()
        self.node.running = False
        self.node.close()
        self.assertEqual(os.read(self.read_fd,5),frame[:5])
        summary = next(row for event,row in self.node.events if event=='uart_pacing_summary')
        self.assertFalse(summary['continuity_valid'])
        self.assertEqual(summary['shutdown_pending_uart_bytes'],len(frame)-5)
        self.assertEqual(summary['radio_to_uart']['charged_bytes'],5)
        self.assertIsNone(self.node.hold_pending)


@unittest.skipUnless(hasattr(os, 'openpty'), 'real Linux PTY and UART setup are unavailable on this host')
class ControlUartPtyTests(unittest.TestCase):
    def test_actual_control_owner_duplex_pty_with_loopback_radio_and_original_bsf1_epoch(self):
        """Production constructor/step, real PTY+UDP; no ns-3, flight controller or hardware claim."""
        import tty
        from pymavlink import mavutil
        master, slave = os.openpty()
        self.addCleanup(os.close, master); self.addCleanup(os.close, slave)
        tty.setraw(slave); os.set_blocking(master, False)
        peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(peer.close)
        peer.bind(('127.0.0.1', 0)); peer.setblocking(False)
        args = build_parser().parse_args(['uav','--run-id','actual-control-pty','--uav-count','1',
            '--uav-id','1','--tty',os.ttyname(slave),'--port',str(peer.getsockname()[1])])
        events = []
        with patch('network.coordination.service.endpoint_ip', lambda i:f'127.0.0.{i+1}'), patch('network.coordination.service.signal.signal'):
            node = UAVService(args, lambda event,**fields:events.append((event,fields)))
            try:
                self.assertEqual(node.uart_timing['owner'], 'software_pty')
                grant = Authority(args.run_id).issue(1,time.monotonic_ns(),2_000_000_000)
                destination = ('127.0.0.2',args.port)
                peer.sendto(encode('grant',0,grant),destination)
                node.step()
                tx = mavutil.mavlink.MAVLink(None,srcSystem=255,srcComponent=190)
                frame = bytes(tx.set_mode_encode(1,1,4).pack(tx,force_mavlink1=False))
                command = make_command(grant,destination_id=1,sequence=1,now_ns=time.monotonic_ns(),payload=frame)
                peer.sendto(encode('command',0,command),destination)
                source = mavutil.mavlink.MAVLink(None,srcSystem=1,srcComponent=1)
                heartbeat = bytes(source.heartbeat_encode(2,3,128,4,4).pack(source,force_mavlink1=False))
                os.write(master,heartbeat)
                output, bodies = bytearray(), []
                deadline = time.monotonic()+1
                while time.monotonic()<deadline and (bytes(output)!=frame or not bodies):
                    node.step()
                    if select.select([master],[],[],0)[0]:
                        output.extend(os.read(master,4096))
                    while select.select([peer],[],[],0)[0]:
                        kind, _, body = decode(peer.recv(1401))
                        if kind=='telemetry': bodies.append(body)
                self.assertEqual(bytes(output),frame)
                self.assertTrue(any(event=='uart_admitted' for event,_ in events))
                self.assertEqual(len(bodies),1)
                receiver = Reassembler(channel='control',uav_id=1,direction='uart_to_gcs')
                self.assertEqual(receiver.ingest(bodies[0]['payload'],time.monotonic_ns()),[heartbeat])
                self.assertEqual(receiver.released_sent_ns,[bodies[0]['producer_ns']])
                self.assertEqual(node.latest_autopilot_mode['received_host_monotonic_ns'],bodies[0]['producer_ns'])
                self.assertEqual(node.uart_up_budget.snapshot()['charged_bytes'],len(heartbeat))
                self.assertEqual(node.uart_down_budget.snapshot()['charged_bytes'],len(frame))
            finally:
                node.close()


if __name__ == '__main__':
    unittest.main()
