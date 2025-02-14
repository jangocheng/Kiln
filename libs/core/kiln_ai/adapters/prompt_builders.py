import json
from abc import ABCMeta, abstractmethod
from enum import StrEnum
from typing import Annotated, Dict

from pydantic import AfterValidator

from kiln_ai.datamodel import Task, TaskRun
from kiln_ai.utils.exhaustive_error import raise_exhaustive_enum_error
from kiln_ai.utils.formatting import snake_case


class BasePromptBuilder(metaclass=ABCMeta):
    """Base class for building prompts from tasks.

    Provides the core interface and basic functionality for prompt builders.
    """

    def __init__(self, task: Task):
        """Initialize the prompt builder with a task.

        Args:
            task (Task): The task containing instructions and requirements.
        """
        self.task = task

    def prompt_id(self) -> str | None:
        """Returns the ID of the prompt, scoped to this builder.

        Returns:
            str | None: The ID of the prompt, or None if not set.
        """
        return None

    def build_prompt(self, include_json_instructions) -> str:
        """Build and return the complete prompt string.

        Returns:
            str: The constructed prompt.
        """
        prompt = self.build_base_prompt()

        if include_json_instructions and self.task.output_schema():
            prompt = (
                prompt
                + f"\n\n# Format Instructions\n\nReturn a JSON object conforming to the following schema:\n```\n{self.task.output_schema()}\n```"
            )

        return prompt

    @abstractmethod
    def build_base_prompt(self) -> str:
        """Build and return the complete prompt string.

        Returns:
            str: The constructed prompt.
        """
        pass

    @classmethod
    def prompt_builder_name(cls) -> str:
        """Returns the name of the prompt builder, to be used for persisting into the datastore.

        Default implementation gets the name of the prompt builder in snake case. If you change the class name, you should override this so prior saved data is compatible.

        Returns:
            str: The prompt builder name in snake_case format.
        """
        return snake_case(cls.__name__)

    def build_user_message(self, input: Dict | str) -> str:
        """Build a user message from the input.

        Args:
            input (Union[Dict, str]): The input to format into a message.

        Returns:
            str: The formatted user message.
        """
        if isinstance(input, Dict):
            return f"The input is:\n{json.dumps(input, indent=2, ensure_ascii=False)}"

        return f"The input is:\n{input}"

    def chain_of_thought_prompt(self) -> str | None:
        """Build and return the chain of thought prompt string.

        Returns:
            str: The constructed chain of thought prompt.
        """
        return None

    def build_prompt_for_ui(self) -> str:
        """Build a prompt for the UI. It includes additional instructions (like chain of thought), even if they are passed to the model in stages.

        Designed for end-user consumption, not for model consumption.

        Returns:
            str: The constructed prompt string.
        """
        base_prompt = self.build_prompt(include_json_instructions=False)
        cot_prompt = self.chain_of_thought_prompt()
        if cot_prompt:
            base_prompt += "\n# Thinking Instructions\n\n" + cot_prompt
        return base_prompt


class SimplePromptBuilder(BasePromptBuilder):
    """A basic prompt builder that combines task instruction with requirements."""

    def build_base_prompt(self) -> str:
        """Build a simple prompt with instruction and requirements.

        Returns:
            str: The constructed prompt string.
        """
        base_prompt = self.task.instruction

        # TODO: this is just a quick version. Formatting and best practices TBD
        if len(self.task.requirements) > 0:
            base_prompt += (
                "\n\nYour response should respect the following requirements:\n"
            )
            # iterate requirements, formatting them in numbereed list like 1) task.instruction\n2)...
            for i, requirement in enumerate(self.task.requirements):
                base_prompt += f"{i + 1}) {requirement.instruction}\n"

        return base_prompt


class MultiShotPromptBuilder(BasePromptBuilder):
    """A prompt builder that includes multiple examples in the prompt."""

    @classmethod
    def example_count(cls) -> int:
        """Get the maximum number of examples to include in the prompt.

        Returns:
            int: The maximum number of examples (default 25).
        """
        return 25

    def build_base_prompt(self) -> str:
        """Build a prompt with instruction, requirements, and multiple examples.

        Returns:
            str: The constructed prompt string with examples.
        """
        base_prompt = f"# Instruction\n\n{self.task.instruction}\n\n"

        if len(self.task.requirements) > 0:
            base_prompt += "# Requirements\n\nYour response should respect the following requirements:\n"
            for i, requirement in enumerate(self.task.requirements):
                base_prompt += f"{i + 1}) {requirement.instruction}\n"
            base_prompt += "\n"

        valid_examples = self.collect_examples()

        if len(valid_examples) == 0:
            return base_prompt

        base_prompt += "# Example Outputs\n\n"
        for i, example in enumerate(valid_examples):
            base_prompt += self.prompt_section_for_example(i, example)

        return base_prompt

    def prompt_section_for_example(self, index: int, example: TaskRun) -> str:
        # Prefer repaired output if it exists, otherwise use the regular output
        output = example.repaired_output or example.output
        return f"## Example {index + 1}\n\nInput: {example.input}\nOutput: {output.output}\n\n"

    def collect_examples(self) -> list[TaskRun]:
        valid_examples: list[TaskRun] = []
        runs = self.task.runs(readonly=True)

        # first pass, we look for repaired outputs. These are the best examples.
        for run in runs:
            if len(valid_examples) >= self.__class__.example_count():
                break
            if run.repaired_output is not None:
                valid_examples.append(run)

        # second pass, we look for high quality outputs (rating based)
        # Minimum is "high_quality" (4 star in star rating scale), then sort by rating
        # exclude repaired outputs as they were used above
        runs_with_rating = [
            run
            for run in runs
            if run.output.rating is not None
            and run.output.rating.value is not None
            and run.output.rating.is_high_quality()
            and run.repaired_output is None
        ]
        runs_with_rating.sort(
            key=lambda x: (x.output.rating and x.output.rating.value) or 0, reverse=True
        )
        for run in runs_with_rating:
            if len(valid_examples) >= self.__class__.example_count():
                break
            valid_examples.append(run)
        return valid_examples


class FewShotPromptBuilder(MultiShotPromptBuilder):
    """A prompt builder that includes a small number of examples in the prompt."""

    @classmethod
    def example_count(cls) -> int:
        """Get the maximum number of examples to include in the prompt.

        Returns:
            int: The maximum number of examples (4).
        """
        return 4


class RepairsPromptBuilder(MultiShotPromptBuilder):
    """A prompt builder that includes multiple examples in the prompt, including repaired instructions describing what was wrong, and how it was fixed."""

    def prompt_section_for_example(self, index: int, example: TaskRun) -> str:
        if (
            not example.repaired_output
            or not example.repair_instructions
            or not example.repaired_output.output
        ):
            return super().prompt_section_for_example(index, example)

        prompt_section = f"## Example {index + 1}\n\nInput: {example.input}\n\n"
        prompt_section += (
            f"Initial Output Which Was Insufficient: {example.output.output}\n\n"
        )
        prompt_section += f"Instructions On How to Improve the Initial Output: {example.repair_instructions}\n\n"
        prompt_section += (
            f"Repaired Output Which is Sufficient: {example.repaired_output.output}\n\n"
        )
        return prompt_section


def chain_of_thought_prompt(task: Task) -> str:
    """Standard implementation to build and return the chain of thought prompt string.

    Returns:
        str: The constructed chain of thought prompt.
    """

    cot_instruction = task.thinking_instruction
    if not cot_instruction:
        cot_instruction = "Think step by step, explaining your reasoning."

    return cot_instruction


class SimpleChainOfThoughtPromptBuilder(SimplePromptBuilder):
    """A prompt builder that includes a chain of thought prompt on top of the simple prompt."""

    def chain_of_thought_prompt(self) -> str | None:
        return chain_of_thought_prompt(self.task)


class FewShotChainOfThoughtPromptBuilder(FewShotPromptBuilder):
    """A prompt builder that includes a chain of thought prompt on top of the few shot prompt."""

    def chain_of_thought_prompt(self) -> str | None:
        return chain_of_thought_prompt(self.task)


class MultiShotChainOfThoughtPromptBuilder(MultiShotPromptBuilder):
    """A prompt builder that includes a chain of thought prompt on top of the multi shot prompt."""

    def chain_of_thought_prompt(self) -> str | None:
        return chain_of_thought_prompt(self.task)


class SavedPromptBuilder(BasePromptBuilder):
    """A prompt builder that looks up a static prompt."""

    def __init__(self, task: Task, prompt_id: str):
        super().__init__(task)
        prompt_model = next(
            (
                prompt
                for prompt in task.prompts(readonly=True)
                if prompt.id == prompt_id
            ),
            None,
        )
        if not prompt_model:
            raise ValueError(f"Prompt ID not found: {prompt_id}")
        self.prompt_model = prompt_model

    def prompt_id(self) -> str | None:
        return self.prompt_model.id

    def build_base_prompt(self) -> str:
        """Returns a saved prompt.

        Returns:
            str: The prompt string.
        """
        return self.prompt_model.prompt

    def chain_of_thought_prompt(self) -> str | None:
        return self.prompt_model.chain_of_thought_instructions


class FineTunePromptBuilder(BasePromptBuilder):
    """A prompt builder that looks up a fine-tune prompt."""

    def __init__(self, task: Task, nested_fine_tune_id: str):
        super().__init__(task)

        # IDs are in project_id::task_id::fine_tune_id format
        self.full_fine_tune_id = nested_fine_tune_id
        parts = nested_fine_tune_id.split("::")
        if len(parts) != 3:
            raise ValueError(
                f"Invalid fine-tune ID format. Expected 'project_id::task_id::fine_tune_id', got: {nested_fine_tune_id}"
            )
        fine_tune_id = parts[2]

        fine_tune_model = next(
            (
                fine_tune
                for fine_tune in task.finetunes(readonly=True)
                if fine_tune.id == fine_tune_id
            ),
            None,
        )
        if not fine_tune_model:
            raise ValueError(f"Fine-tune ID not found: {fine_tune_id}")
        self.fine_tune_model = fine_tune_model

    def prompt_id(self) -> str | None:
        return self.full_fine_tune_id

    def build_base_prompt(self) -> str:
        return self.fine_tune_model.system_message

    def chain_of_thought_prompt(self) -> str | None:
        return self.fine_tune_model.thinking_instructions


# Generators that can take any task and build a prompt
class PromptGenerators(StrEnum):
    SIMPLE = "simple_prompt_builder"
    MULTI_SHOT = "multi_shot_prompt_builder"
    FEW_SHOT = "few_shot_prompt_builder"
    REPAIRS = "repairs_prompt_builder"
    SIMPLE_CHAIN_OF_THOUGHT = "simple_chain_of_thought_prompt_builder"
    FEW_SHOT_CHAIN_OF_THOUGHT = "few_shot_chain_of_thought_prompt_builder"
    MULTI_SHOT_CHAIN_OF_THOUGHT = "multi_shot_chain_of_thought_prompt_builder"


prompt_generator_values = [pg.value for pg in PromptGenerators]


# Our prompt ID can be one of:
# - A saved prompt ID
# - A fine-tune prompt ID
# - A prompt generator name
PromptId = Annotated[
    str,
    AfterValidator(lambda v: _check_prompt_id(v)),
]
"""
A pydantic type that validates strings containing a valid prompt ID. 
"""


def _check_prompt_id(id: str) -> str:
    """
    Check that the prompt ID is valid.
    """
    if id in prompt_generator_values:
        return id

    if id.startswith("id::"):
        # check it has 4 parts divided by :: -- 'id::project_id::task_id::prompt_id'
        parts = id.split("::")
        if len(parts) != 4:
            raise ValueError(
                f"Invalid saved prompt ID: {id}. Expected format: 'id::[project_id]::[task_id]::[prompt_id]'."
            )
        return id

    if id.startswith("fine_tune_prompt::"):
        # check it had a fine_tune_id after the :: -- 'fine_tune_prompt::fine_tune_id'
        fine_tune_id = id[18:]
        if len(fine_tune_id) == 0:
            raise ValueError(
                f"Invalid fine-tune prompt ID: {id}. Expected format: 'fine_tune_prompt::[fine_tune_id]'."
            )
        return id

    raise ValueError(f"Invalid prompt ID: {id}")


# Our UI has some names that are not the same as the class names, which also hint parameters.
def prompt_builder_from_id(prompt_id: str, task: Task) -> BasePromptBuilder:
    """Convert a name used in the UI to the corresponding prompt builder class.

    Args:
        prompt_id (str): The prompt ID.

    Returns:
        type[BasePromptBuilder]: The corresponding prompt builder class.

    Raises:
        ValueError: If the UI name is not recognized.
    """

    # Saved prompts are prefixed with "id::"
    if prompt_id.startswith("id::"):
        prompt_id = prompt_id[4:]
        return SavedPromptBuilder(task, prompt_id)

    # Fine-tune prompts are prefixed with "fine_tune_prompt::"
    if prompt_id.startswith("fine_tune_prompt::"):
        prompt_id = prompt_id[18:]
        return FineTunePromptBuilder(task, prompt_id)

    # Check if the prompt_id matches any enum value
    if prompt_id not in [member.value for member in PromptGenerators]:
        raise ValueError(f"Unknown prompt generator: {prompt_id}")
    typed_prompt_generator = PromptGenerators(prompt_id)

    match typed_prompt_generator:
        case PromptGenerators.SIMPLE:
            return SimplePromptBuilder(task)
        case PromptGenerators.FEW_SHOT:
            return FewShotPromptBuilder(task)
        case PromptGenerators.MULTI_SHOT:
            return MultiShotPromptBuilder(task)
        case PromptGenerators.REPAIRS:
            return RepairsPromptBuilder(task)
        case PromptGenerators.SIMPLE_CHAIN_OF_THOUGHT:
            return SimpleChainOfThoughtPromptBuilder(task)
        case PromptGenerators.FEW_SHOT_CHAIN_OF_THOUGHT:
            return FewShotChainOfThoughtPromptBuilder(task)
        case PromptGenerators.MULTI_SHOT_CHAIN_OF_THOUGHT:
            return MultiShotChainOfThoughtPromptBuilder(task)
        case _:
            # Type checking will find missing cases
            raise_exhaustive_enum_error(typed_prompt_generator)
