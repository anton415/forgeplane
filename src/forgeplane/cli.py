from pathlib import Path
from typing import Annotated, Literal, TypeAlias, TypedDict
import json

import typer
import yaml
from rich.console import Console
from rich.table import Table

# app хранит все команды CLI: scan, generate и будущие команды.
app = typer.Typer(help="Forgeplane — инструмент для работы с API-спецификациями")

# Rich нужен для красивого текстового отчёта в терминале.
console = Console()

# Разрешаем только три значения для --format.
OutputFormat: TypeAlias = Literal["json", "text", "yaml"]


class ScanReport(TypedDict):
    """Структура отчёта, который возвращает scan_docs."""

    path: str
    files_count: int
    total_size_bytes: int
    extensions: dict[str, int]
    files: list[str]


def scan_docs(path: Path) -> ScanReport:
    # rglob("*") рекурсивно проходит по всем файлам и папкам внутри path.
    files = sorted(item for item in path.rglob("*") if item.is_file())

    # Здесь собираем статистику: расширения файлов и общий размер.
    extensions: dict[str, int] = {}
    total_size = 0

    for file in files:
        # Если у файла нет расширения, записываем его в отдельную группу.
        suffix = file.suffix.lower() or "no_extension"
        extensions[suffix] = extensions.get(suffix, 0) + 1
        total_size += file.stat().st_size

    # Возвращаем обычный словарь, но его форма описана типом ScanReport.
    return {
        "path": str(path),
        "files_count": len(files),
        "total_size_bytes": total_size,
        "extensions": extensions,
        "files": [str(file.relative_to(path)) for file in files],
    }


def print_text_report(report: ScanReport) -> None:
    # Table из Rich рисует аккуратную таблицу в терминале.
    table = Table(title="Forgeplane scan report")
    table.add_column("Metric")
    table.add_column("Value")

    table.add_row("Path", report["path"])
    table.add_row("Files", str(report["files_count"]))
    table.add_row("Total size", f'{report["total_size_bytes"]} bytes')
    table.add_row(
        "Extensions",
        ", ".join(f"{ext}: {count}" for ext, count in report["extensions"].items()) or "none",
    )

    console.print(table)

    if report["files"]:
        console.print("\nFiles:")
        for file in report["files"]:
            console.print(f"  - {file}")


@app.callback()
def main() -> None:
    # Callback нужен, чтобы Typer сделал приложение с подкомандами:
    # forgeplane scan ..., forgeplane generate ...
    """Forgeplane CLI."""


@app.command()
def generate(
    url: Annotated[str, typer.Argument(help="URL для генерации спецификации")],
    output: Annotated[
        str,
        typer.Option("--output", "-o", help="Файл вывода"),
    ] = "spec.yaml",
) -> None:
    """Генерирует API-спецификацию."""
    typer.echo(f"Генерирую спецификацию для {url} в {output}...")


@app.command()
def scan(
    path: Annotated[
        Path,
        typer.Argument(
            # Typer сам проверит, что путь существует и это папка, а не файл.
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
            help="Путь к папке с документацией",
        ),
    ],
    output_format: Annotated[
        OutputFormat,
        typer.Option(
            # Пользователь может написать --format json или коротко -f json.
            "--format",
            "-f",
            case_sensitive=False,
            help="Формат отчёта: json, text или yaml",
        ),
    ] = "text",
) -> None:
    """Сканирует папку документации и печатает отчёт."""
    report = scan_docs(path)

    # Один и тот же отчёт можно вывести в разных форматах.
    if output_format == "json":
        typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    elif output_format == "yaml":
        typer.echo(yaml.safe_dump(report, allow_unicode=True, sort_keys=False))
    else:
        print_text_report(report)
