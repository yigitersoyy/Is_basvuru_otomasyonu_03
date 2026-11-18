import json
import os
from pathlib import Path

import typer
import uvicorn
import yaml
from pydantic import ValidationError

from .catalog import import_manual
from .db import Store, stamp
from .documents import find_font
from .maintenance import backup, cleanup, restore
from .matching import refresh
from .models import benchmark
from .profile import import_facts, load_profile, save_profile
from .queue import schedule, worker
from .settings import Problem, Settings, allowed_url
from .sources import board_url

cli = typer.Typer(no_args_is_help=True, add_completion=False)
profiles = typer.Typer(no_args_is_help=True)
cli.add_typer(profiles, name="profile")


def get_store():
    store = Store(Settings())
    store.initialize()
    return store


@cli.command()
def doctor():
    """Veri dizinini, PDF fontunu ve kaynak izinlerini denetle."""
    store = get_store()
    if os.name != "nt":
        for path in (store.root, store.blobs, store.path, store.ledger_path):
            if path.stat().st_mode & 0o077:
                raise Problem(f"Yalnızca kullanıcı erişimi verin: {path.name}")
    find_font(store.settings)
    sources = store.settings.sources()
    for source in sources:
        if source.platform not in {"fixture", "manual"} and source.read:
            allowed_url(board_url(source), source.hosts)
    with store.connect() as conn:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise Problem("SQLite bütünlük denetimi başarısız.")
    mode = store.settings
    typer.echo(f"SQLite ve PDF fontu hazır. {len(sources)} kaynak. {mode.discovery_mode} / {mode.submit_mode} / {mode.llm_backend}")


@cli.command("login-token")
def token():
    """Yerel panelin giriş kodunu göster."""
    from .web import login_token
    typer.echo(login_token(get_store().session_key()))


@cli.command()
def demo():
    """Boş deneme profiline sentetik olgular ve ilanlar ekle."""
    store = get_store()
    if store.settings.discovery_mode != "fixture":
        raise Problem("Örnek veri için DISCOVERY_MODE=fixture ayarlayın.")
    with store.connect(write=True) as conn:
        if not load_profile(conn)["id"]:
            facts = [
                {"kind": "experience", "text": "REST API geliştirme ve bakım", "source": "Sentetik deneyim kaydı",
                 "start": "2020-01-01", "end": "2025-01-01", "verified_at": stamp(), "language": "tr"},
                {"kind": "project", "text": "Python, FastAPI, SQL, Docker ve PostgreSQL ile veri hatları geliştirme",
                 "source": "Sentetik proje kaydı", "verified_at": stamp(), "language": "tr"},
                {"kind": "skill", "text": "Python, SQL, Git ve Docker", "source": "Sentetik beceri kaydı",
                 "verified_at": stamp(), "language": "und"},
                {"kind": "certification", "text": "Örnek doğrulanmamış sertifika", "source": "Sentetik taslak kayıt"},
            ]
            prefs = [{"kind": "title", "value": "Backend"}, {"kind": "remote_region", "value": "TR", "required": True}]
            save_profile(conn, {"name": "Deniz Öztürk", "email": "deniz@example.invalid"}, facts, prefs)
            conn.execute("INSERT OR IGNORE INTO control VALUES ('demo_profile', '1')")
    schedule(store, only="fixture", force=True)
    worker(store, once=True)
    typer.echo("Örnek ilanlar hazır.")


@profiles.command("import")
def import_profile(file: Path):
    """CV metnini doğrulanmayı bekleyen olgulara dönüştür."""
    facts = import_facts(file.name, file.read_bytes())
    store = get_store()
    with store.connect(write=True) as conn:
        current = load_profile(conn)
        save_profile(conn, current["identity"], current["facts"] + facts, current["preferences"])
    typer.echo(f"{len(facts)} taslak olgu eklendi. Doğrulamak için Profili açın.")


@profiles.command("preferences")
def preferences(file: Path):
    """YAML dosyasından çalışma tercihlerini kaydet."""
    data = yaml.safe_load(file.read_text(encoding="utf-8"))
    store = get_store()
    with store.connect(write=True) as conn:
        current = load_profile(conn)
        save_profile(conn, current["identity"], current["facts"], data["preferences"])
        refresh(conn)
    typer.echo("Tercihler kaydedildi.")


@cli.command()
def discover(source: str | None = None, enqueue_only: bool = False):
    """Zamanı gelen kaynakları tara."""
    store = get_store()
    total = schedule(store, only=source)
    if not enqueue_only:
        worker(store, once=True)
    typer.echo(f"{total} tarama görevi sıraya alındı.")


@cli.command("import-notice")
def import_notice(file: Path):
    """Kullanıcının dışarı aktardığı ilan bildirimlerini YAML veya JSON olarak al."""
    data = yaml.safe_load(file.read_text(encoding="utf-8"))
    entries = data if isinstance(data, list) else [data]
    store = get_store()
    with store.connect(write=True) as conn:
        ids = {import_manual(conn, item) for item in entries}
        refresh(conn)
    typer.echo(f"{len(ids)} fırsat içe aktarıldı.")


@cli.command("match")
def match_jobs():
    """Güncel profil ve ilanlarla eşleşmeleri hesapla."""
    with get_store().connect(write=True) as conn:
        count = refresh(conn)
    typer.echo(f"{count} ilan değerlendirildi.")


@cli.command("worker")
def run_worker(once: bool = False):
    """Kalıcı görev kuyruğunu işle."""
    worker(get_store(), once=once)


@cli.command()
def web():
    """Yerel paneli başlat."""
    from .web import create_app
    store = get_store()
    uvicorn.run(create_app(store), host=store.settings.dashboard_bind, port=store.settings.dashboard_port,
                access_log=False, proxy_headers=False, log_level="info")


@cli.command("backup-key")
def backup_key():
    """Şifreli yedek için yeni anahtar üret."""
    from cryptography.fernet import Fernet
    typer.echo(Fernet.generate_key().decode())


@cli.command("backup")
def take_backup(destination: Path | None = None):
    """SQLite ve belge deposunu şifreli olarak yedekle."""
    store = get_store()
    destination = destination or store.root / "backups" / f"isakisi-{stamp().replace(':', '').replace('+', 'Z')}.enc"
    backup(store, destination, os.getenv("ISAKISI_BACKUP_KEY"))
    typer.echo(f"Yedek oluşturuldu. {destination}")


@cli.command("restore")
def restore_backup(source: Path, services_stopped: bool = typer.Option(False, "--services-stopped")):
    """Panel ve işçiyi durdurduktan sonra şifreli yedeği geri yükle."""
    if not services_stopped:
        raise Problem("Paneli ve işçiyi durdurup --services-stopped kullanın.")
    restore(get_store(), source, os.getenv("ISAKISI_BACKUP_KEY"))
    typer.echo("Yedek geri yüklendi. Başvuru yolu manuel moda alındı.")


@cli.command("resume-submission")
def resume_submission(verified_employer_history: bool = typer.Option(False, "--verified-employer-history")):
    """İşveren kayıtlarıyla uzlaştırma tamamlanınca yedek dönüş kilidini aç."""
    if not verified_employer_history:
        raise Problem("Yedek tarihinden bu yana yapılan başvuruları işveren kayıtlarıyla doğrulayın.")
    store = get_store()
    with store.connect(write=True) as conn:
        if conn.execute("""SELECT 1 FROM applications WHERE state IN
                         ('queued','submitting','partial','submission_unknown') LIMIT 1""").fetchone():
            raise Problem("Bekleyen başvuruları önce panelden uzlaştırın.")
        conn.execute("DELETE FROM control WHERE key='restore_lock'")
    typer.echo("Başvurularda yapılandırılan gönderim yolu yeniden kullanılabilir.")


@cli.command("cleanup")
def cleanup_old():
    """Saklama süresi dolan verileri temizle."""
    cleanup(get_store())


@cli.command("benchmark")
def run_benchmark(file: Path, e5: bool = False, ollama: bool = False):
    """Etiketli ilanlar üzerinde kural sıralaması ve isteğe bağlı modelleri ölç."""
    store = get_store()
    settings = store.settings
    if ollama and settings.llm_backend != "ollama":
        raise Problem("Ölçüm için LLM_BACKEND=ollama ayarlayın.")
    result = benchmark(json.loads(file.read_text(encoding="utf-8")), e5, store.root / "e5-cache.json",
                       settings if ollama else None)
    typer.echo(json.dumps(result, indent=2))


def main():
    if os.name != "nt":
        os.umask(0o077)
    try:
        cli()
    except Problem as error:
        typer.echo(str(error), err=True)
        raise SystemExit(1) from None
    except (OSError, ValidationError, yaml.YAMLError, KeyError, TypeError, ValueError):
        typer.echo("Dosya, izin veya yapılandırma kontrolü gerekli.", err=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
