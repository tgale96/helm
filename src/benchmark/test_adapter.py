import shutil
import tempfile
from typing import List

from .adapter_service import AdapterService
from .scenario import CORRECT_TAG, create_scenario, Instance, Reference
from .run_specs import get_scenario_spec1, get_adapter_spec1
from .adapter import ADAPT_GENERATION, ADAPT_LANGUAGE_MODELING, ADAPT_MULTIPLE_CHOICE_JOINT, Adapter, AdapterSpec
from common.authentication import Authentication
from proxy.server_service import ServerService


class TestAdapter:
    def setup_method(self):
        self.path: str = tempfile.mkdtemp()
        service = ServerService(base_path=self.path, root_mode=True)
        self.adapter_service = AdapterService(service, Authentication("test"))

    def teardown_method(self, method):
        shutil.rmtree(self.path)

    def test_adapter1(self):
        scenario = create_scenario(get_scenario_spec1())
        adapter_spec = get_adapter_spec1()
        scenario_state = Adapter(adapter_spec, self.adapter_service).adapt(scenario.get_instances())

        # Make sure we generated the right number of request_states:
        # For each trial, instance and reference (+ 1 for free-form generation).
        num_instances = len(scenario_state.instances)
        assert num_instances * adapter_spec.num_train_trials == len(scenario_state.request_states)

    def test_construct_prompt(self):
        adapter_spec = AdapterSpec(
            model="openai/davinci", method=ADAPT_GENERATION, input_prefix="", output_prefix="", max_tokens=100
        )
        adapter = Adapter(adapter_spec, self.adapter_service)
        correct_reference = Reference(output="", tags=[CORRECT_TAG])
        train_instances: List[Instance] = [Instance(input="train", references=[correct_reference]) for _ in range(2049)]
        eval_instances = Instance(input="eval", references=[])
        prompt: str = adapter.construct_prompt(
            train_instances, eval_instances, include_output=False, reference_index=None
        )

        # Ensure the prompt fits within the context window
        assert adapter.tokenizer.fits_within_context_window(prompt)

        # Ensure the in-context examples were removed before touching the evaluation instance
        assert prompt.endswith("eval")

    def test_construct_prompt_with_truncation(self):
        adapter_spec = AdapterSpec(
            model="openai/davinci", method=ADAPT_GENERATION, input_prefix="", output_prefix="", max_tokens=100
        )
        adapter = Adapter(adapter_spec, self.adapter_service)
        correct_reference = Reference(output="", tags=[CORRECT_TAG])
        train_instances: List[Instance] = [Instance(input="train", references=[correct_reference]) for _ in range(100)]
        eval_instances = Instance(input="eval" * 2049, references=[])
        prompt: str = adapter.construct_prompt(
            train_instances, eval_instances, include_output=False, reference_index=None
        )

        # Ensure the prompt fits within the context window
        assert adapter.tokenizer.fits_within_context_window(prompt)

        # Ensure that all the in-context examples were completely removed and we had to truncate the eval Instance input
        assert "train" not in prompt
        assert prompt.count("eval") == 1948

    def test_construct_language_modeling_prompt(self):
        model: str = "openai/davinci"
        adapter_spec = AdapterSpec(
            method=ADAPT_LANGUAGE_MODELING, input_prefix="", model=model, output_prefix="", max_tokens=0,
        )
        adapter = Adapter(adapter_spec, self.adapter_service)

        # The tokens translate to: '�Excuse me�'
        conditioning_tokens, pred_tokens = [110, 40127], [1904, 502, 447]
        prompt, num_conditioning_tokens = adapter.construct_language_modeling_prompt(
            conditioning_tokens=conditioning_tokens, pred_tokens=pred_tokens, max_req_len=5, text=""
        )

        # Ensure the prompt is correct
        assert prompt == "Excuse me"

        # Ensure the number of conditioning tokens is correct
        assert num_conditioning_tokens == 1

    def test_sample_examples(self):
        adapter_spec = AdapterSpec(method=ADAPT_MULTIPLE_CHOICE_JOINT, max_train_instances=4)
        adapter = Adapter(adapter_spec, self.adapter_service)
        all_train_instances = [
            Instance("say no", references=[Reference("no", tags=[CORRECT_TAG])]),
            Instance("say yes1", references=[Reference("yes", tags=[CORRECT_TAG])]),
            Instance("say yes2", references=[Reference("yes", tags=[CORRECT_TAG])]),
            Instance("say yes3", references=[Reference("yes", tags=[CORRECT_TAG])]),
            Instance("say yes4", references=[Reference("yes", tags=[CORRECT_TAG])]),
        ]

        examples = adapter.sample_examples(all_train_instances, seed=0)
        assert len(examples) == 4

        # An instance with "say yes" should have be sampled first before "say no"
        assert examples[0].input == "say yes4"
        assert examples[1].input == "say no"
        assert examples[2].input == "say yes1"
        assert examples[3].input == "say yes3"

    def test_sample_examples_no_train_instances(self):
        adapter_spec = AdapterSpec(method=ADAPT_MULTIPLE_CHOICE_JOINT, max_train_instances=2)
        adapter = Adapter(adapter_spec, self.adapter_service)
        examples = adapter.sample_examples(all_train_instances=[], seed=0)
        assert len(examples) == 0

    def test_sample_examples_greater_max_train_instances(self):
        adapter_spec = AdapterSpec(method=ADAPT_MULTIPLE_CHOICE_JOINT, max_train_instances=10)
        adapter = Adapter(adapter_spec, self.adapter_service)
        all_train_instances = [
            Instance("say no", references=[Reference("no", tags=[CORRECT_TAG])]),
            Instance("say yes", references=[Reference("yes", tags=[CORRECT_TAG])]),
            Instance("say yes", references=[Reference("yes", tags=[CORRECT_TAG])]),
        ]

        examples = adapter.sample_examples(all_train_instances, seed=0)
        assert len(examples) == 3

    def test_sample_examples_without_references(self):
        adapter_spec = AdapterSpec(method=ADAPT_LANGUAGE_MODELING, max_train_instances=1)
        adapter = Adapter(adapter_spec, self.adapter_service)
        all_train_instances = [
            Instance("prompt1", references=[]),
            Instance("prompt2", references=[]),
            Instance("prompt3", references=[]),
        ]

        examples = adapter.sample_examples(all_train_instances, seed=0)
        assert len(examples) == 1

    def test_fits_tokens_within_context_window(self):
        model: str = "openai/davinci"
        adapter_spec = AdapterSpec(
            method=ADAPT_LANGUAGE_MODELING, input_prefix="", model=model, output_prefix="", max_tokens=0,
        )
        adapter = Adapter(adapter_spec, self.adapter_service)

        # The tokens translate to: '<|endoftext|>The the the the ... the the'
        # There are 1 `conditioning_token` and 2049 `pred_tokens`. Since the `max_request_length`
        # of GPT-3 is 2049, calling `fits_tokens_within_context_window` will remove the last `pred_token`
        conditioning_tokens, pred_tokens = [50256], [464] + [262] * 2048
        prompt, pred_tokens = adapter.fits_tokens_within_context_window(
            conditioning_tokens, pred_tokens, adapter.tokenizer.max_request_length
        )

        # Ensure the prompt is correct
        assert prompt == "<|endoftext|>The" + " the" * 2047

        # Ensure the pred_tokens are correct
        assert pred_tokens == [464] + [262] * 2047
