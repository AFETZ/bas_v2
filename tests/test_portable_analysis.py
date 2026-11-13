"""Host-side analysis adapter tests; no Docker, GPU or host pymavlink required."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bas
from scripts.product import bootstrap_native_runtime as native


class PortableAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch = patch.object(bas,"ROOT",self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.run = "closed-1"
        self.folder = bas.control_dir(self.run)
        self.raw = bas.raw_dir(self.run)
        self.folder.mkdir(parents=True)
        self.raw.mkdir(parents=True)
        self.record = dict(run_id=self.run,status="completed",runtime_status="completed",
                           started_host_monotonic_ns=1,ended_host_monotonic_ns=2,returncode=0,
                           measurement_validity="invalid")
        self.config = dict(runtime=dict(image="sha256:"+"a"*64))
        bas.write_json(self.folder/"run.json",self.record)
        bas.write_json(self.folder/"config.json",self.config)
        analyzer = self.root/"scripts/product/analyze_relay_experiment.py"
        analyzer.parent.mkdir(parents=True)
        analyzer.write_text("# fixture analyzer source\n")
        self.output = Path("runs/analysis/attempt-1")

    def call(self, container=None, *, service_batch=False):
        def complete(*args,**kwargs):
            out=kwargs["output"]
            (out/"metrics").mkdir()
            (out/"metrics"/("service_series_analysis.json" if service_batch else "relay_analysis.json")).write_text(json.dumps(dict(run_status=self.record["status"])))
        with patch.object(native,"image_id",return_value=self.config["runtime"]["image"]),              patch.object(native,"container",side_effect=container or complete) as invoke,              patch.object(bas.subprocess,"check_output",return_value="b"*40+"\n"):
            result=bas.analyze(self.run,self.output,service_batch=service_batch)
        return result,invoke

    def test_complete_analysis_retains_input_grade_and_uses_existing_readonly_runner(self):
        before=(self.folder/"run.json").read_bytes()
        code,invoke=self.call()
        self.assertEqual(code,0)
        self.assertEqual((self.folder/"run.json").read_bytes(),before)
        receipt=json.loads((self.root/self.output/"analysis-attempt.json").read_text())
        self.assertEqual(receipt["status"],"completed")
        self.assertEqual(receipt["analyzed_measurement_validity"],"invalid")
        args,kwargs=invoke.call_args
        self.assertEqual(args[:2],(self.root,self.config["runtime"]["image"]))
        self.assertTrue(kwargs["readonly"])
        self.assertEqual(kwargs["supplementary_groups"],(1000,))
        self.assertIn("--no-plots --skip-live-export",args[2])
        self.assertIn("/home/ubuntu/.local/lib/python3.10/site-packages",args[2])

    def test_failed_closed_run_can_be_analyzed_without_becoming_successful_run(self):
        self.record.update(status="failed",runtime_status="failed",returncode=1)
        bas.write_json(self.folder/"run.json",self.record)
        code,_=self.call()
        result=json.loads((self.root/self.output/"metrics/relay_analysis.json").read_text())
        self.assertEqual(code,0)
        self.assertEqual(result["run_status"],"failed")

    def test_open_foreign_and_missing_end_records_never_start_container_or_create_output(self):
        for changes in [dict(status="running"),dict(run_id="other"),dict(ended_host_monotonic_ns=None),
                        dict(runtime_status="running"),dict(ended_host_monotonic_ns=True),
                        dict(returncode=None)]:
            with self.subTest(changes=changes):
                bas.write_json(self.folder/"run.json",dict(self.record,**changes))
                with patch.object(native,"container") as invoke, patch.object(native,"image_id") as image:
                    with self.assertRaisesRegex(ValueError,"closed terminal"):
                        bas.analyze(self.run,self.output)
                    invoke.assert_not_called()
                    image.assert_not_called()
                self.assertFalse((self.root/self.output).exists())

    def test_output_cannot_reuse_existing_attempt_escape_analysis_or_shadow_raw(self):
        for output in [Path("runs/control/rewrite"),Path("../outside"),Path("runs/analysis")]:
            with self.subTest(output=output):
                self.output=output
                with self.assertRaises(ValueError):
                    self.call()
        self.output=Path("runs/analysis/attempt-1")
        self.call()
        with self.assertRaisesRegex(ValueError,"new directory"):
            self.call()
        (self.root/"runs/analysis/raw-link").symlink_to(self.raw,target_is_directory=True)
        self.output=Path("runs/analysis/raw-link/new")
        with self.assertRaisesRegex(ValueError,"raw or control"):
            self.call()
        self.assertFalse((self.raw/"new").exists())

    def test_container_failure_and_changed_input_keep_owned_failed_attempt(self):
        def failed(*args,**kwargs):
            (kwargs["output"]/"partial.txt").write_text("retained")
            raise RuntimeError("fixture container failed")
        with self.assertRaisesRegex(RuntimeError,"fixture"):
            self.call(failed)
        receipt=json.loads((self.root/self.output/"analysis-attempt.json").read_text())
        self.assertEqual(receipt["status"],"failed")
        self.assertTrue((self.root/self.output/"partial.txt").is_file())
        self.output=Path("runs/analysis/attempt-2")
        def changed(*args,**kwargs):
            bas.write_json(self.folder/"run.json",dict(self.record,late_mutation=True))
        with self.assertRaisesRegex(ValueError,"metadata changed"):
            self.call(changed)
        receipt=json.loads((self.root/self.output/"analysis-attempt.json").read_text())
        self.assertEqual(receipt["status"],"failed")

    def test_mutable_image_tag_is_rejected_before_an_attempt_is_created(self):
        bas.write_json(self.folder/"config.json",dict(runtime=dict(image="image:latest")))
        with self.assertRaisesRegex(ValueError,"immutable runtime image"):
            self.call()
        self.assertFalse((self.root/self.output).exists())

    def test_closed_failed_batch_uses_ledger_without_requiring_a_batch_raw_directory(self):
        ledger = dict(schema_version=1,batch_id=self.run,status="failed",returncode=1,
                      started_host_monotonic_ns=1,ended_host_monotonic_ns=2,
                      config=self.config,cases=[dict(run_id="attempted"),dict(run_id="not-started")])
        bas.write_json(self.folder/"batch.json",ledger)
        self.raw.rmdir()
        code,invoke=self.call(service_batch=True)
        self.assertEqual(code,0)
        command=invoke.call_args.args[2]
        self.assertIn("--service-batch",command)
        self.assertIn("/runs/control/closed-1/batch.json",command)
        self.assertNotIn("--skip-live-export",command)
        receipt=json.loads((self.root/self.output/"analysis-attempt.json").read_text())
        self.assertEqual(receipt["analyzed_batch_status"],"failed")
        self.assertIsNone(receipt["analyzed_runtime_status"])
        self.assertEqual([row["path"] for row in receipt["source_bindings"]],
                         ["runs/control/closed-1/batch.json"])
        self.assertEqual(json.loads((self.folder/"batch.json").read_text()),ledger)

    def test_batch_running_or_missing_end_never_creates_analysis_attempt(self):
        for status,end in [("running",2),("completed",None)]:
            bas.write_json(self.folder/"batch.json",
                dict(batch_id=self.run,status=status,returncode=0,
                     started_host_monotonic_ns=1,ended_host_monotonic_ns=end,config=self.config))
            with self.assertRaisesRegex(ValueError,"closed terminal"):
                self.call(service_batch=True)
            self.assertFalse((self.root/self.output).exists())

    def test_existing_docker_builder_has_no_gpu_and_only_selected_output_writable(self):
        output=self.root/self.output
        output.mkdir(parents=True)
        argv=native.docker_command(self.root,self.config["runtime"]["image"],"true",
                                  readonly=True,output=output,supplementary_groups=(1000,))
        self.assertIn("--read-only",argv)
        self.assertIn("--network",argv)
        self.assertEqual(argv[argv.index("--network")+1],"none")
        self.assertIn(str(self.root.resolve())+":"+native.MOUNT+":ro",argv)
        self.assertEqual(argv[argv.index("--group-add")+1],"1000")
        self.assertEqual(argv[argv.index("--mount")+1],
                         f"type=bind,src={output.resolve()},dst=/bas-analysis-output")
        self.assertNotIn("--gpus",argv)
        with self.assertRaises(ValueError):
            native.docker_command(self.root,self.config["runtime"]["image"],"true",
                                  readonly=False,output=output)


if __name__ == "__main__":
    unittest.main()
