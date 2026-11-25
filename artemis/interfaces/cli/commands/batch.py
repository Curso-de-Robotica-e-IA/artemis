# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Batch tasks execution command (artemis batch)."""

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated

from rich.console import Console
from rich.table import Table
import typer

from artemis.config import initialize_llm_config
from artemis.sdk import Agent
from artemis.sdk.builders import Builders
from third_party.mobile_use.sdk.types.task import AgentProfile
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)


async def run_batch_tasks(
    tasks: list[str],
    profile_name: str = "pro",
    delay_seconds: float = 5.0,
    verification_level: str | None = None,
    explorer_pro_mode: str | None = None,
) -> None:
    """Executes a list of automation tasks sequentially.

    Args:
        tasks: Goals to run one after another on a single agent.
        profile_name: Execution profile ('flash' or 'pro') applied to every goal.
        delay_seconds: Pause between successive goals.
        verification_level: Coarse Checker preset ('off', 'final', 'checkpoints',
            'strict') for the Pro profile; ignored by Flash.
        explorer_pro_mode: Explorer tier ('flash', 'pro', 'ultra') behind
            ``ask_explorer`` under the Pro profile; ignored by Flash.
    """
    if not os.environ.get("ARTEMIS_TASK_INGRESS"):
        os.environ["ARTEMIS_TASK_INGRESS"] = "cli"
    llm_config = initialize_llm_config()
    profile = AgentProfile(name="default", llm_config=llm_config)
    config_builder = Builders.AgentConfig.with_default_profile(profile)
    if verification_level is not None:
        config_builder.with_verification_level(verification_level)
    if explorer_pro_mode is not None:
        config_builder.with_explorer(pro_mode=explorer_pro_mode)
    config = config_builder.build()

    agent = Agent(config=config)
    await agent.init()

    console = Console()
    results = []

    try:
        for idx, goal in enumerate(tasks, start=1):
            console.rule(f"[bold cyan]Task {idx}/{len(tasks)}: {goal}[/bold cyan]")
            status = "SUCCESS"
            error_msg = ""
            try:
                result = await agent.run_task(goal=goal, profile=profile_name)
                logger.info(f"Task {idx} completed: {result}")
            except Exception as e:
                status = "FAILED"
                error_msg = str(e)
                logger.error(f"Task {idx} failed: {e}")

            results.append({"task": goal, "status": status, "error": error_msg})

            if idx < len(tasks) and delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
    finally:
        await agent.clean()

    # Print summary table
    table = Table(title="Batch Execution Summary")
    table.add_column("#", justify="right", style="cyan")
    table.add_column("Task Goal", style="white")
    table.add_column("Status", justify="center")
    table.add_column("Notes", style="red")

    for i, res in enumerate(results, 1):
        status_styled = (
            "[bold green]PASS[/bold green]"
            if res["status"] == "SUCCESS"
            else "[bold red]FAIL[/bold red]"
        )
        table.add_row(str(i), res["task"], status_styled, res["error"])

    console.print(table)


def batch_command(
    tasks_file: Annotated[
        Path | None,
        typer.Option(
            "--file",
            "-f",
            help="Path to a text or JSON file containing task goals (one per line or JSON array).",
        ),
    ] = None,
    goals: Annotated[
        list[str] | None,
        typer.Argument(
            help="Task goals specified directly on the command line.",
        ),
    ] = None,
    profile: Annotated[
        str,
        typer.Option(
            "--profile",
            "-p",
            help="Execution profile to use for all tasks ('flash' or 'pro').",
        ),
    ] = "pro",
    delay: Annotated[
        float,
        typer.Option(
            "--delay",
            "-d",
            help="Delay in seconds between successive tasks.",
        ),
    ] = 5.0,
    standalone: Annotated[
        bool,
        typer.Option(
            "--standalone",
            help="Run in standalone embedded mode (default; kept for CLI compatibility).",
        ),
    ] = False,
    verification_level: Annotated[
        str | None,
        typer.Option(
            "--verification-level",
            help=(
                "Checker preset for the Pro profile, applied to every goal: 'off' (no audit),"
                " 'final' (exit review only, the default), 'checkpoints' (every plan"
                " checkpoint + exit review), 'strict' (checkpoints with a larger repair"
                " budget; a failed assert halts)."
            ),
        ),
    ] = None,
    explorer_pro_mode: Annotated[
        str | None,
        typer.Option(
            "--explorer-pro-mode",
            help="Explorer tier behind ask_explorer under the Pro profile ('flash', 'pro', 'ultra').",
        ),
    ] = None,
) -> None:
    """Execute multiple automation tasks in sequence."""
    _ = standalone
    task_list: list[str] = []

    if tasks_file:
        if not tasks_file.exists():
            typer.secho(f"Error: Tasks file '{tasks_file}' not found.", fg=typer.colors.RED)
            raise typer.Exit(1)

        content = tasks_file.read_text(encoding="utf-8").strip()
        if tasks_file.suffix.lower() == ".json":
            try:
                parsed = json.loads(content)
                if isinstance(parsed, list):
                    task_list.extend(str(item) for item in parsed)
                else:
                    typer.secho(
                        "Error: JSON file must contain a list of goals.", fg=typer.colors.RED
                    )
                    raise typer.Exit(1)
            except Exception as e:
                typer.secho(f"Error parsing JSON file: {e}", fg=typer.colors.RED)
                raise typer.Exit(1)
        else:
            task_list.extend(
                [
                    line.strip()
                    for line in content.splitlines()
                    if line.strip() and not line.startswith("#")
                ]
            )

    if goals:
        task_list.extend(goals)

    if not task_list:
        typer.secho(
            "Error: No tasks provided. Use --file or pass goals as arguments.", fg=typer.colors.RED
        )
        raise typer.Exit(1)

    asyncio.run(
        run_batch_tasks(
            task_list,
            profile_name=profile,
            delay_seconds=delay,
            verification_level=verification_level,
            explorer_pro_mode=explorer_pro_mode,
        )
    )
