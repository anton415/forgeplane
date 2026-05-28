import typer

app = typer.Typer(help="SpecForge — инструмент для работы с API-спецификациями")

@app.command()
def generate(
    url: str = typer.Argument(..., help="URL для генерации спецификации"),
    output: str = typer.Option("spec.yaml", "--output", "-o", help="Файл вывода"),
):
    """Генерирует API-спецификацию."""
    typer.echo(f"Генерирую спецификацию для {url}...")

if __name__ == "__main__":
    app()