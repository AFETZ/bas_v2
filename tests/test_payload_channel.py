"""Actual UDP/BSF1/MAVLink integration; run with target pymavlink dependencies."""
import json
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


if __name__ == '__main__':
    unittest.main()
