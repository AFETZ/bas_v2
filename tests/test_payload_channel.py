"""Actual UDP/BSF1/MAVLink integration; run with target pymavlink dependencies."""
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch
from collections import deque

from network.coordination.payload import PayloadChannel
from network.scripts.serial_transport import (Encoder, Reassembler, BoundedQueue,
    parse_mavlink_with_provenance, mavlink_payload_lineage)
from scripts.product import town01_full_stack_scenario as town


class PayloadChannelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.addCleanup(patch.stopall)
        patch.object(town, 'GCS_IP', '127.0.0.1').start()
        patch.object(town, 'endpoint_ip', lambda index: '127.0.0.1').start()

    def helper(self, ids=(1,), **kwargs):
        helper = PayloadChannel(self.folder, self.folder / 'node_state.json', ids, **kwargs)
        self.addCleanup(helper.close)
        return helper

    def test_payload_only_binds_no_control_socket_and_reports_absence(self):
        helper = self.helper((1, 3))
        self.assertEqual(set(helper.harness.sockets), {'payload'})
        self.assertEqual(set(helper.harness.transport_encoders), {('payload', 1), ('payload', 3)})
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
            control.bind(('127.0.0.1', 14600))
        self.assertFalse(helper.snapshot()['all_uavs_bidirectional_observed'])
        result = helper.close()
        self.assertEqual(result['hardware_validation'], 'not_performed')
        self.assertIsNone(result['uavs']['1']['last_attitude_age_s'])
        self.assertEqual(helper.close(), result)

    def exchange(self, window):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
            peer.bind(('127.0.0.1', 14701))
            peer.settimeout(1)
            helper = self.helper()
            if window in ('inside', 'late'):
                start = time.monotonic_ns()
                end = start + (1_000_000 if window == 'late' else 2_000_000_000)
                helper.start_measurement(start, end)
                if window == 'late':
                    # Windows GetTickCount64 may not advance during a 5 ms sleep.
                    # Establish the test's post-window precondition in the clock
                    # domain actually used by production observations.
                    deadline = time.perf_counter() + 1
                    while time.monotonic_ns() < end and time.perf_counter() < deadline:
                        time.sleep(.001)
                    self.assertGreaterEqual(time.monotonic_ns(), end)
            from pymavlink import mavutil
            parser = mavutil.mavlink.MAVLink(None)
            transmitter = mavutil.mavlink.MAVLink(None)
            transmitter.srcSystem, transmitter.srcComponent = 1, 1
            receiver = Reassembler(channel='payload', uav_id=1, direction='gcs_to_uart')
            # Force fragmentation in the returning path, with real CRC and parser.
            encoder = Encoder(channel='payload', uav_id=1, direction='uart_to_gcs', max_payload=8)
            helper.pump()
            received_types = []
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and 'COMMAND_LONG' not in received_types:
                data, source = peer.recvfrom(65535)
                for record in receiver.ingest(data):
                    for message in parser.parse_buffer(record) or []:
                        received_types.append(message.get_type())
                        if message.get_type() == 'COMMAND_LONG':
                            self.assertEqual(message.command, mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL)
                            self.assertEqual(message.param1, mavutil.mavlink.MAVLINK_MSG_ID_ATTITUDE)
                            self.assertEqual(message.target_system, 1)
            self.assertEqual(set(received_types), {'HEARTBEAT', 'COMMAND_LONG'})
            replies = [transmitter.heartbeat_encode(mavutil.mavlink.MAV_TYPE_QUADROTOR,
                           mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA, 0, 0, mavutil.mavlink.MAV_STATE_STANDBY),
                       transmitter.command_ack_encode(mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,
                           mavutil.mavlink.MAV_RESULT_ACCEPTED),
                       transmitter.attitude_encode(12345, .1, .2, .3, 0., 0., 0.)]
            for reply in replies:
                frame = reply.pack(transmitter, force_mavlink1=False)
                for chunk in encoder.encode(frame):
                    peer.sendto(chunk, source)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                helper.pump()
                if helper.snapshot()['all_uavs_bidirectional_observed']:
                    break
                time.sleep(.001)
            if window == 'preflight':
                start = time.monotonic_ns()
                helper.start_measurement(start, start + 2_000_000_000)
            metrics = helper.close()
            self.assertTrue(metrics['all_uavs_bidirectional_observed'])
            self.assertEqual(metrics['uavs']['1']['attitudes_received'], 1)
            self.assertEqual(metrics['uavs']['1']['last_attitude_autopilot_time_boot_ms'], 12345)
            self.assertGreater(metrics['transport']['payload:uav1']['uart_output_bytes'], 0)
            saved = json.loads((self.folder / 'metrics/payload_channel.json').read_text())
            self.assertEqual(saved, metrics)
            return metrics

    def test_real_udp_exchange_inside_window_is_counted(self):
        metrics = self.exchange('inside')
        window = metrics['measurement_window']
        self.assertTrue(window['all_bidirectional_observed'])
        self.assertEqual(window['service_sla'], 'not_evaluated')
        for stat in window['uavs']['1']['observations'].values():
            self.assertEqual(stat['count'], 1)
            self.assertGreaterEqual(stat['first_host_monotonic_ns'], window['start_host_monotonic_ns'])
            self.assertLess(stat['last_host_monotonic_ns'], window['end_host_monotonic_ns'])
            self.assertIsNone(stat['max_internal_gap_s'])
            self.assertGreaterEqual(stat['max_observed_gap_s_including_edges'], 0)

    def test_preflight_exchange_cannot_qualify_measurement_window(self):
        metrics = self.exchange('preflight')
        self.assertTrue(metrics['all_uavs_bidirectional_observed'])
        window = metrics['measurement_window']
        self.assertFalse(window['all_bidirectional_observed'])
        self.assertTrue(all(stat['count'] == 0 for stat in window['uavs']['1']['observations'].values()))

    def test_late_postwindow_exchange_is_excluded(self):
        metrics = self.exchange('late')
        self.assertTrue(metrics['all_uavs_bidirectional_observed'])
        window = metrics['measurement_window']
        self.assertFalse(window['all_bidirectional_observed'])
        self.assertEqual(window['status'], 'closed')
        self.assertTrue(all(stat['count'] == 0 for stat in window['uavs']['1']['observations'].values()))

    def test_wrong_roster_frame_cannot_count_as_payload_telemetry(self):
        with (socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer,
              socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as valid_peer):
            peer.bind(('127.0.0.1', 14702))
            # Keep the legitimate UAV1 destination alive while injecting UAV2.
            # An absent destination causes Windows to surface an unrelated ICMP
            # port-unreachable as WinError10054 on the next receive/close pump.
            valid_peer.bind(('127.0.0.1', 14701))
            helper = self.helper((1,))
            from pymavlink import mavutil
            transmitter = mavutil.mavlink.MAVLink(None)
            transmitter.srcSystem = 2
            frame = transmitter.heartbeat_encode(mavutil.mavlink.MAV_TYPE_QUADROTOR,
                mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA, 0, 0, mavutil.mavlink.MAV_STATE_STANDBY).pack(transmitter)
            encoder = Encoder(channel='payload', uav_id=2, direction='uart_to_gcs')
            for chunk in encoder.encode(frame):
                peer.sendto(chunk, ('127.0.0.1', 14700))
            helper.pump()
            metrics = helper.close()
            self.assertFalse(metrics['all_uavs_bidirectional_observed'])
            self.assertGreater(metrics['transport']['malformed_datagrams'], 0)

    def test_ack_received_before_our_request_cannot_prove_downlink(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
            peer.bind(('127.0.0.1', 14701))
            helper = self.helper()
            from pymavlink import mavutil
            transmitter = mavutil.mavlink.MAVLink(None)
            transmitter.srcSystem = 1
            frame = transmitter.command_ack_encode(mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,
                mavutil.mavlink.MAV_RESULT_ACCEPTED).pack(transmitter)
            encoder = Encoder(channel='payload', uav_id=1, direction='uart_to_gcs')
            for chunk in encoder.encode(frame):
                peer.sendto(chunk, ('127.0.0.1', 14700))
            helper.pump()
            metrics = helper.close()
            self.assertEqual(metrics['uavs']['1']['accepted_interval_acknowledgements'], 0)
            self.assertFalse(metrics['all_uavs_bidirectional_observed'])

    def test_invalid_roster_and_timing_are_rejected_before_binding(self):
        for ids in ((), (1, 1), (0,), (6,), (True,)):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.helper(ids)
        with self.assertRaises(ValueError):
            PayloadChannel(self.folder, self.folder / 'state.json', (1,), interval_s=float('nan'))

    def test_current_default_has_no_invented_application_lineage(self):
        with patch.dict('os.environ', {'BAS_NATIVE_EVENT_LOGGING':'metrics_only'}):
            metrics=self.helper().close()
        self.assertEqual(metrics['application_lineage']['status'],'not_evaluated_logging_disabled')
        self.assertFalse((self.folder/'payload/logs/scenario_events.jsonl').exists())

    def test_software_udp_uplink_lineage_survives_reorder_duplicate_corrupt_and_missing_tail(self):
        """Development E2E only: real UDP/shared framing/parser; no RF or UART hardware."""
        from network.scripts.serial_transport import decode_chunk
        from scripts.product.town01_full_stack_scenario import append_jsonl
        from scripts.product.analyze_relay_experiment import analyze_payload_lineage
        with (socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as ingress,
              socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as source,
              socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as forward):
            ingress.bind(('127.0.0.1',0));ingress.settimeout(2)
            forward.bind(('127.0.0.1',14701))
            helper=self.helper(event_logging='batched_trace')
            log=self.folder/'logs/payload_uart_uav1.jsonl'
            epoch='software-uart-boundary'
            def event(name,**fields):
                append_jsonl(log,dict(schema=1,event=name,run_id=self.folder.name,process_epoch=epoch,
                    channel='payload',uav_id=1,clock_domain='host_monotonic',
                    observed_host_monotonic_ns=time.monotonic_ns(),**fields))
            event('payload_lineage_started')
            start=time.monotonic_ns();end=start+100_000_000
            helper.start_measurement(start,end)
            mav=helper.harness.mavutil.mavlink
            tx=mav.MAVLink(None,srcSystem=1,srcComponent=1)
            parser=mav.MAVLink(None);parser.robust_parsing=True
            pending=deque();encoder=Encoder(channel='payload',uav_id=1,direction='uart_to_gcs',max_payload=8)
            for slot in range(3):
                frame=bytes(tx.attitude_encode(12345+slot,.1,.2,.3,0.,0.,0.).pack(tx,force_mavlink1=False))
                produced=time.monotonic_ns();sequence=encoder.sequence
                for message,spans in parse_mavlink_with_provenance(parser,pending,frame,(sequence,produced)):
                    meta=mavlink_payload_lineage(message,spans,run_id=self.folder.name,uav_id=1,direction='uav_to_gcs')
                    event('mavlink_frame',payload_lineage_stage='offered',payload_direction='uav_to_gcs',
                        created_host_monotonic_ns=produced,deadline_host_monotonic_ns=produced+1_000_000_000,**meta)
                fragments=encoder.encode(frame,produced)
                for packet in reversed(fragments):
                    source.sendto(packet,ingress.getsockname())
                    transmitted=time.monotonic_ns()
                    packet,_=ingress.recvfrom(65535)
                    chunk=decode_chunk(packet)
                    event('payload_record_sent',sequence=chunk.sequence,producer_host_monotonic_ns=chunk.sent_monotonic_ns,
                        fragment_index=chunk.fragment_index,fragment_count=chunk.fragment_count,
                        record_crc32=chunk.record_crc32,record_bytes=chunk.total_length,
                        transmitted_host_monotonic_ns=transmitted)
                    # A development forwarding boundary imposes faults after
                    # actual source UDP send; it is not a radio-model result.
                    if slot==2 and chunk.fragment_index==chunk.fragment_count-1:
                        continue
                    if slot==1 and chunk.fragment_index==0:
                        packet=packet[:-1]+bytes([packet[-1]^1])
                    forward.sendto(packet,('127.0.0.1',14700))
                    if slot==0:
                        forward.sendto(packet,('127.0.0.1',14700))
                    helper.pump()
            time.sleep(1.05)  # Observe the complete 1s deadline horizon, not an imputed loss.
            event('payload_lineage_closed',close_error=None)
            helper.close()
            result=analyze_payload_lineage(self.folder,start,end,complete=True)
            uplink=[row for row in result['records'] if row['direction']=='uav_to_gcs']
            self.assertEqual(len(uplink),3,result)
            self.assertEqual([row['outcome'] for row in uplink],['timely','missing','missing'])
            self.assertEqual(sum(row['mavlink_body_bytes'] for row in uplink),3*frame[1])
            self.assertEqual(len({row['frame_uid'] for row in uplink}),3)
            self.assertEqual(helper.snapshot()['uavs']['1']['attitudes_received'],1)
            self.assertEqual(result['service_class_availability'],'not_evaluated_requires_qualified_load_and_series')


class PayloadByteProvenanceTests(unittest.TestCase):
    def setUp(self):
        from pymavlink.dialects.v20 import ardupilotmega
        self.mav=ardupilotmega
        tx=self.mav.MAVLink(None,srcSystem=1,srcComponent=1)
        self.frame=bytes(tx.attitude_encode(12345,.1,.2,.3,0.,0.,0.).pack(tx,force_mavlink1=False))

    def parser(self):
        value=self.mav.MAVLink(None);value.robust_parsing=True
        return value

    def lineage(self,message,spans):
        return mavlink_payload_lineage(message,spans,run_id='software-byte-probe',uav_id=1,direction='uav_to_gcs')

    def test_coalesced_identical_frames_and_fragmented_records_keep_original_ids(self):
        parser,pending=self.parser(),deque()
        records=[self.frame[:7],self.frame[7:]+self.frame]
        times=[10_000_000,20_000_000]
        original=[]
        for sequence,(record,produced) in enumerate(zip(records,times)):
            original.extend(self.lineage(message,spans) for message,spans in
                parse_mavlink_with_provenance(parser,pending,record,(sequence,produced)) if message.get_type()!='BAD_DATA')
        self.assertEqual(len(original),2)
        self.assertEqual(original[0]['frame_sha256'],original[1]['frame_sha256'])
        self.assertNotEqual(original[0]['frame_uid'],original[1]['frame_uid'])
        encoder=Encoder(channel='payload',uav_id=1,direction='uart_to_gcs',max_payload=5)
        receiver=Reassembler(channel='payload',uav_id=1,direction='uart_to_gcs')
        parser,pending=self.parser(),deque();released=[]
        for record,produced in zip(records,times):
            fragments=encoder.encode(record,produced)
            for fragment in reversed(fragments):
                output=receiver.ingest(fragment,30_000_000)
                for data,sequence,stamp in zip(output,receiver.released_sequences,receiver.released_sent_ns):
                    released.extend(self.lineage(message,spans) for message,spans in
                        parse_mavlink_with_provenance(parser,pending,data,(sequence,stamp)) if message.get_type()!='BAD_DATA')
            receiver.ingest(fragments[0],30_000_000)  # Real duplicate BSF1 datagram.
        self.assertEqual(released,original)
        self.assertGreater(receiver.counters.duplicate_chunks,0)
        self.assertEqual(sum(r['mavlink_body_bytes'] for r in released),2*self.frame[1])
        self.assertEqual(list(pending),[])

    def test_partial_writes_canonicalize_to_original_frame_without_timestamp_refresh(self):
        original=self.lineage(self.parser().parse_buffer(self.frame)[0],[((7,10_000_000),0,len(self.frame))])
        queue=BoundedQueue(2,1024,1.)
        self.assertTrue(queue.put(self.frame,.01,(7,10_000_000,0)))
        parser,pending=self.parser(),deque();observed=[]
        for count in (3,5,len(self.frame)-8):
            data=queue.items[0][0];tag=queue.metadata[0]
            observed.extend(self.lineage(message,spans) for message,spans in
                parse_mavlink_with_provenance(parser,pending,data[:count],tag) if message.get_type()!='BAD_DATA')
            queue.metadata[0]=(tag[0],tag[1],tag[2]+count)
            queue.sent(count)
        self.assertEqual(observed,[original])
        self.assertEqual(list(queue.items),[])
        self.assertEqual(list(queue.metadata),[])

    def test_expired_partial_frame_overflow_and_clear_do_not_deliver_missing_tail(self):
        queue=BoundedQueue(1,1024,.1)
        parser,pending=self.parser(),deque()
        queue.put(self.frame,0.,(1,0,0))
        self.assertEqual(parse_mavlink_with_provenance(parser,pending,self.frame[:5],queue.metadata[0]),[])
        queue.metadata[0]=(1,0,5);queue.sent(5)
        self.assertFalse(queue.put(self.frame,.01,(2,10_000_000,0)))
        self.assertEqual(len(queue.metadata),len(queue.items))
        queue.expire(.101)
        self.assertEqual(list(queue.metadata),[])
        self.assertEqual(list(queue.items),[])
        queue.put(self.frame,.11,(3,110_000_000,0));queue.clear()
        self.assertEqual(list(queue.metadata),[])
        # A reconnect creates a fresh parser/epoch; the old partial bytes cannot
        # be combined with a new source record or counted as a received frame.
        parser,pending=self.parser(),deque()
        output=parse_mavlink_with_provenance(parser,pending,self.frame,(0,200_000_000))
        self.assertEqual(len(output),1)
        self.assertEqual(self.lineage(*output[0])['source_first_host_monotonic_ns'],200_000_000)


@unittest.skipUnless(os.name == 'posix' and hasattr(os, 'openpty'),
    'production dual-PTY UART software boundary requires POSIX; physical hardware is not tested')
class PayloadUartAdapterPosixTests(unittest.TestCase):
    def test_production_control_and_payload_pty_trace_preserves_both_directions(self):
        """Two real adapter processes/PTYs/UDP sockets; no simulated RF or hardware SLA."""
        from contextlib import ExitStack
        import selectors
        import subprocess
        import sys
        import tty
        from pymavlink.dialects.v20 import ardupilotmega as mavlink
        from network.scripts.serial_transport import decode_chunk, CHANNEL_IDS

        root = Path(__file__).resolve().parents[1]
        with ExitStack() as resources:
            resources.enter_context(patch.dict(os.environ, {'BAS_SIMULATION_MODE': 'realtime', 'MAVLINK20': '1'}))
            artifacts = os.environ.get('BAS_EXTERNAL_TEST_ARTIFACT_DIR')
            if artifacts:
                run = Path(artifacts).resolve() / 'dual_pty_payload'
                run.mkdir(parents=True, exist_ok=False)  # Never overwrite another attempt.
            else:
                run = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix='bas-payload-pty-')))
            logs = run / 'logs'; logs.mkdir()
            channels, commands = {}, {}

            def close_fd(descriptor):
                os.close(descriptor)

            def stop_owned(process):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait(timeout=2)

            for channel in ('control', 'payload'):
                master, slave = os.openpty()
                resources.callback(close_fd, slave); resources.callback(close_fd, master)
                tty.setraw(slave); os.set_blocking(master, False)
                radio = resources.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
                radio.bind(('127.0.0.1', 0)); radio.setblocking(False)
                ready = logs / (channel + '.ready.json')
                event = logs / (channel + '_uart_uav1.jsonl')
                metrics = logs / (channel + '.metrics.json')
                output = logs / (channel + '.process.log')
                stream = resources.enter_context(output.open('xb'))
                argv = [sys.executable, str(root/'network/scripts/communication_vertical.py'), 'uart-adapter',
                    '--channel', channel, '--tty', os.ttyname(slave), '--bind', '127.0.0.1:0',
                    '--peer', f'127.0.0.1:{radio.getsockname()[1]}', '--event-log', str(event),
                    '--ready-file', str(ready), '--metrics-output', str(metrics), '--framed',
                    '--chunk-payload-bytes', '8', '--event-logging', 'batched_trace']
                process = subprocess.Popen(argv, cwd=root, stdin=subprocess.DEVNULL, stdout=stream,
                                           stderr=subprocess.STDOUT, env=os.environ.copy(), close_fds=True)
                resources.callback(stop_owned, process)
                channels[channel] = dict(master=master, radio=radio, process=process, output=output,
                    ready=ready, event=event, metrics=metrics, records=[], chunks=[], uplink_lineage=[],
                    parser=mavlink.MAVLink(None), pending=deque(),
                    decoder=Reassembler(channel=channel, uav_id=1, direction='uart_to_gcs'))
                channels[channel]['parser'].robust_parsing = True
                commands[channel] = dict(pid=process.pid, argv=argv)
            (run/'commands.json').write_text(json.dumps(commands, indent=2)+'\n')

            def ensure_running():
                for channel, state in channels.items():
                    self.assertIsNone(state['process'].poll(),
                        f'{channel} adapter stopped: {state["output"].read_text()}')

            deadline = time.monotonic() + 5
            while not all(state['ready'].exists() for state in channels.values()):
                ensure_running(); self.assertLess(time.monotonic(), deadline, 'Both UART adapters did not become ready')
                time.sleep(.005)
            ensure_running()
            for channel, state in channels.items():
                ready = json.loads(state['ready'].read_text())
                self.assertEqual((ready['pid'], ready['channel'], ready['uav_id']),
                                 (state['process'].pid, channel, 1))
                self.assertEqual(ready['baud_rate'], 115200)
                self.assertEqual(ready['tos'], 184 if channel == 'control' else 40)
                self.assertEqual(ready['simulation_mode'], 'realtime')

            uav = mavlink.MAVLink(None, srcSystem=1, srcComponent=1)
            gcs = mavlink.MAVLink(None, srcSystem=255, srcComponent=190)
            control_up = bytes(uav.heartbeat_encode(mavlink.MAV_TYPE_QUADROTOR,
                mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA, 0, 0, mavlink.MAV_STATE_STANDBY).pack(uav))
            payload_up = bytes(uav.attitude_encode(12345, .1, .2, .3, 0., 0., 0.).pack(uav))
            control_down = bytes(gcs.command_long_encode(1, 1, mavlink.MAV_CMD_REQUEST_MESSAGE, 0,
                float(mavlink.MAVLINK_MSG_ID_AUTOPILOT_VERSION), 0, 0, 0, 0, 0, 0).pack(gcs))
            payload_down = bytes(gcs.heartbeat_encode(mavlink.MAV_TYPE_GCS,
                mavlink.MAV_AUTOPILOT_INVALID, 0, 0, mavlink.MAV_STATE_ACTIVE).pack(gcs))
            uplink = {'control': control_up, 'payload': payload_up*2}
            downlink = {'control': control_down, 'payload': payload_down*2}
            # Both adapters are live before either direction is exercised. Learn
            # their ephemeral bind ports from real datagrams instead of racing
            # reservation/rebind of fixed test ports.
            for channel, state in channels.items():
                self.assertEqual(os.write(state['master'], uplink[channel]), len(uplink[channel]))
            with selectors.DefaultSelector() as selector:
                for channel, state in channels.items(): selector.register(state['radio'], selectors.EVENT_READ, channel)
                deadline = time.monotonic() + 3
                while any(len(b''.join(s['records'])) < len(uplink[c]) for c,s in channels.items()):
                    ensure_running(); self.assertLess(time.monotonic(), deadline, 'UART uplinks did not reach BSF1 receivers')
                    for key, _ in selector.select(.02):
                        channel, state = key.data, channels[key.data]
                        packet, address = state['radio'].recvfrom(65535)
                        self.assertEqual(state.setdefault('adapter_address', address), address)
                        chunk = decode_chunk(packet); state['chunks'].append(chunk)
                        self.assertEqual(chunk.channel_id, CHANNEL_IDS[channel])
                        records = state['decoder'].ingest(packet, time.monotonic_ns())
                        for record, sequence, stamp in zip(records, state['decoder'].released_sequences,
                                                          state['decoder'].released_sent_ns, strict=True):
                            state['records'].append(record)
                            for message, spans in parse_mavlink_with_provenance(state['parser'], state['pending'], record, (sequence,stamp)):
                                self.assertNotEqual(message.get_type(), 'BAD_DATA')
                                if channel == 'payload':
                                    state['uplink_lineage'].append(mavlink_payload_lineage(message, spans,
                                        run_id=run.name, uav_id=1, direction='uav_to_gcs'))
            for channel, state in channels.items():
                self.assertEqual(b''.join(state['records']), uplink[channel])
                self.assertEqual(list(state['pending']), [])
            self.assertNotEqual(channels['control']['adapter_address'], channels['payload']['adapter_address'])

            expected_down = []
            for channel, state in channels.items():
                produced = time.monotonic_ns()
                encoder = Encoder(channel=channel, uav_id=1, direction='gcs_to_uart', max_payload=8)
                state['downlink_produced_ns'] = produced
                packets = encoder.encode(downlink[channel], produced)
                for packet in packets:
                    self.assertEqual(state['radio'].sendto(packet, state['adapter_address']), len(packet))
                if channel == 'payload':
                    parser = mavlink.MAVLink(None); parser.robust_parsing = True
                    expected_down = [mavlink_payload_lineage(message, spans, run_id=run.name, uav_id=1,
                        direction='gcs_to_uav') for message, spans in parse_mavlink_with_provenance(
                            parser, deque(), downlink[channel], (0,produced))]
            received = {channel: bytearray() for channel in channels}
            with selectors.DefaultSelector() as selector:
                for channel, state in channels.items(): selector.register(state['master'], selectors.EVENT_READ, channel)
                deadline = time.monotonic() + 3
                while any(len(received[c]) < len(downlink[c]) for c in channels):
                    ensure_running(); self.assertLess(time.monotonic(), deadline, 'BSF1 downlinks did not reach actual PTYs')
                    for key, _ in selector.select(.02):
                        received[key.data].extend(os.read(key.fileobj, 4096))
            for channel in channels: self.assertEqual(bytes(received[channel]), downlink[channel])

            # Graceful shutdown flushes the real buffered trace. Kill is only an
            # owned-PID cleanup fallback on failure, never the success path.
            for state in channels.values(): state['process'].terminate()
            for state in channels.values():
                self.assertEqual(state['process'].wait(timeout=2), 0, state['output'].read_text())
            result = dict(scope='POSIX production adapter with two PTYs and localhost UDP; no RF, physical UART or SLA qualification',
                          uart_acceptance='os.write return; physical final wire bit is not measured', channels={})
            payload_rows = None
            for channel, state in channels.items():
                metrics = json.loads(state['metrics'].read_text())
                self.assertEqual(metrics['uart_input_bytes'], len(uplink[channel]))
                self.assertEqual(metrics['uart_output_bytes'], len(downlink[channel]))
                rows = [json.loads(line) for line in state['event'].read_text().splitlines()]
                result['channels'][channel] = dict(pid=state['process'].pid, bytes_up=len(uplink[channel]),
                    bytes_down=len(downlink[channel]), metrics=metrics)
                if channel == 'payload': payload_rows = rows
                else: self.assertFalse(any('frame_uid' in row for row in rows), 'Control trace inherited payload metadata')
            offers = [r for r in payload_rows if r.get('payload_lineage_stage')=='offered']
            accepts = [r for r in payload_rows if r.get('payload_lineage_stage')=='accepted']
            self.assertEqual([r['frame_uid'] for r in offers], [r['frame_uid'] for r in channels['payload']['uplink_lineage']])
            self.assertEqual([r['frame_uid'] for r in accepts], [r['frame_uid'] for r in expected_down])
            self.assertEqual(len({r['frame_uid'] for r in offers}), 2)
            self.assertEqual(len({r['frame_uid'] for r in accepts}), 2)
            for row in offers + accepts:
                self.assertEqual(row['run_id'], run.name)
                self.assertTrue(row['provenance_valid'])
                self.assertEqual(sum(r['length'] for r in row['record_references']), row['frame_bytes'])
            for row in accepts:
                self.assertEqual(row['endpoint'], 'uart_os_write_end')
                self.assertEqual(row['accepted_host_monotonic_ns'], row['observed_host_monotonic_ns'])
                self.assertGreaterEqual(row['accepted_host_monotonic_ns'], channels['payload']['downlink_produced_ns'])
            sent = [r for r in payload_rows if r.get('event')=='payload_record_sent']
            self.assertEqual({(r['sequence'],r['producer_host_monotonic_ns'],r['fragment_index']) for r in sent},
                {(c.sequence,c.sent_monotonic_ns,c.fragment_index) for c in channels['payload']['chunks']})
            self.assertEqual([r['event'] for r in payload_rows if r['event'].startswith('payload_lineage_')],
                             ['payload_lineage_started','payload_lineage_closed'])
            self.assertIsNone(payload_rows[-1]['close_error'])
            result['payload_offers'], result['payload_acceptances'] = offers, accepts
            (run/'result.json').write_text(json.dumps(result, indent=2)+'\n')
            print(json.dumps(dict(probe='production_dual_pty_payload_trace', bytes={c:{'up':len(uplink[c]),
                'down':len(downlink[c])} for c in channels}, payload_offered_uids=[r['frame_uid'] for r in offers],
                payload_accepted_uids=[r['frame_uid'] for r in accepts], hardware_validation='not_performed')))


if __name__ == '__main__':
    unittest.main()
