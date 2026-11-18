# İş Akışı

İş Akışı, açık iş ilanlarını yerelde toplar, aday profiliyle karşılaştırır ve başvuru paketini hazırlar. Türkçe panelden ilanın neden önerildiğini, hangi koşulların belirsiz kaldığını, gönderilecek belgeleri ve başvuru geçmişini görebilirsiniz. Python 3.12 ve SQLite üzerinde çalışır.

## Windows'ta ilk çalıştırma

Python 3.12 ve `uv` kurulu olmalı. Proje dizininde PowerShell açın:

```powershell
uv sync --frozen --group dev
$env:APP_DATA_DIR = Join-Path $env:LOCALAPPDATA 'IsAkisi-Demo'
$env:DISCOVERY_MODE = 'fixture'
$env:SUBMIT_MODE = 'simulate'
uv run alembic upgrade head
uv run isakisi doctor
uv run isakisi demo
uv run isakisi login-token
uv run isakisi web
```

[Yerel paneli açın](http://127.0.0.1:8765) ve terminaldeki giriş kodunu kullanın. **Python Backend Geliştirici** ilanını açın, paketi hazırlayın ve **Onayla ve simüle et** düğmesini kullanın. İkinci bir PowerShell penceresinde aynı proje dizininden işçiyi başlatın, ardından başvuru sayfasını yenileyin:

```powershell
$env:APP_DATA_DIR = Join-Path $env:LOCALAPPDATA 'IsAkisi-Demo'
uv run isakisi worker
```

Linux'ta aynı komutlar kullanılabilir. PowerShell ayar satırlarının karşılığı `export APP_DATA_DIR="$HOME/.local/share/isakisi-demo" DISCOVERY_MODE=fixture SUBMIT_MODE=simulate` komutudur. PDF için Windows'ta Arial, Linux'ta DejaVu Sans kullanılır. Farklı bir Türkçe uyumlu TrueType fontu `PDF_FONT` ile seçebilirsiniz.

## Gerçek ilanlarla çalışma

Örnek profilden ayrı bir veri dizini seçin. Proje kökündeki yerel `.env` dosyasına aşağıdaki ayarları kendi mutlak yollarınızla yazın. Panel ve işçi aynı ayarları kullanır. Oturum ortam değişkenleri `.env` değerlerinden önce gelir.

```dotenv
APP_DATA_DIR=C:/Users/BEN/AppData/Local/IsAkisi
SOURCE_CONFIG=C:/Users/BEN/AppData/Local/IsAkisi/sources.yaml
DISCOVERY_MODE=live
SUBMIT_MODE=manual
DASHBOARD_BIND=127.0.0.1
APP_TIMEZONE=Europe/Istanbul
LLM_BACKEND=off
```

1. [Örnek kaynak dosyasını](config/sources.example.yaml) kopyalayın. We Work Remotely beslemesi hazırdır. Greenhouse, Lever ve Ashby için şirketi, pano adını ve alan adlarını girin, izin dayanağını kontrol edin ve `read: true` yapın. İzinli şirket sayfası, kariyer RSS'i veya site haritası için `platform: jsonld` ve `url` kullanın. Her alan adını izin listesine ekleyin. WWR kayıtlarında kaynak ve özgün bağlantı korunur.
2. `uv run alembic upgrade head`, `uv run isakisi doctor` ve `uv run isakisi discover` komutlarını çalıştırın. Panel ve işçiyi ayrı terminallerde başlatın.
3. **Profil** ekranında ad ve iletişim bilgilerini girin. PDF, DOCX veya UTF-8 metin aktarın, her olguyu kaynak metniyle doğrulayın ve tercihleri kaydedin. Metin katmanı olan PDF'ler okunur. Taranmış belgeler için metin aktarımı veya elle giriş kullanılır. Ücret koşulu örneği `60000|EUR|year|gross`.
4. **İlanlar** ekranından uygunluğu inceleyin veya elle ilan ekleyin. Dışarı aktardığınız bildirimler `uv run isakisi import-notice bildirim.yaml` ile içe alınabilir. YAML girdisinde `title`, `company`, `url` ve `description` alanlarını kullanın. Bir liste birden fazla ilanı kaydeder.
5. Paketi hazırlayın, PDF/DOCX ve yanıtları önizleyin, onaylayın ve resmî formu açın. Sonucu **Başvurular** ekranında kaydedin. Geçmiş bir başvuru için ilan sayfasındaki **Önceden başvurdum** seçeneğini kullanın.

## Gönderim yolları

- **Manuel**. Onaylanan paketi indirin. İşverenin formunu tarayıcınızda tamamlayıp sonucu panelde teyit edin.
- **Simülasyon**. Sentetik bir ilan için onayı yerel bir sahte ATS işler. Simülasyon makbuzu, işveren makbuzundan ayrı kaydedilir.
- **İzinli form doldurma**. İşverenin izin kaydı olan kaynak için `prefill: true` ve `prefill_fields` altında sayfadaki tam alan etiketlerini tanımlayın. Anahtarlar `name`, `first_name`, `last_name`, `email`, `phone` ve `resume` olabilir. `uv sync --frozen --extra browser` ve `uv run playwright install chromium` çalıştırın, `SUBMIT_MODE=authorized` kullanın ve işçiyi grafik oturumda başlatın. Tarayıcı yaklaşık üç dakika açık kalır. Form doğrulamasını ve gönderimi kullanıcı tamamlar.
- **Yetkili Greenhouse API**. İşverenin verdiği Job Board API yetkisiyle kaynakta `submit: true` ve örneğin `secret_env: ISAKISI_ACME_KEY` ayarlayın. Anahtarı işçi ve panelin işletim sistemi ortamından yükleyin. `SUBMIT_MODE=authorized` kullanın. Gönderimden hemen önce API form şeması onaylanan şemayla karşılaştırılır. Desteklenmeyen form alanları için resmî form kullanılabilir. Lever, özel başvuru sorularını ilan API'sinden sunmadığı için barındırılan form üzerinden çalışır.

Onay paket sürümüne ve hedefe bağlıdır, 24 saat geçerlidir. Yanıtı kaybolan gönderim **Sonuç belirsiz** durumuna geçer. İşveren kaydını kontrol edip sonucu panelden girin. İşçi aynı isteği otomatik olarak yeniden göndermez.

## Linux zamanlayıcısı ve yedek

`deploy/systemd` birimleri proje konumunu `~/.local/lib/isakisi`, ayar dosyasını `~/.config/isakisi/service.env` olarak kullanır. Gerekirse birimlerde yolları değiştirin. `service.env` içine canlı çalışma ayarlarını ve yedek anahtarını yerleştirin. Keyfi komut içermeyen bir çevre dosyası kullanın ve dosya izinlerini `0600` yapın. Veri dizini uygulama kullanıcısına ait olmalı.

```bash
uv sync --frozen
uv run alembic upgrade head
uv run isakisi doctor
mkdir -p ~/.config/systemd/user
cp deploy/systemd/* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now isakisi-web.service isakisi-worker.service isakisi-discover.timer
# ISAKISI_BACKUP_KEY ayarlandıktan sonra:
systemctl --user enable --now isakisi-backup.timer
```

Tarama 00.17, 06.17, 12.17 ve 18.17'de çalışır. Kaçırılan zamanlama bir sonraki servis açılışında telafi edilir. İstanbul dışında `APP_TIMEZONE` ve timer dosyasındaki saat dilimini birlikte güncelleyin. Servis günlükleri `journalctl --user -u isakisi-worker` ile okunur ve journald döndürmesini kullanır.

`uv run isakisi backup-key` ile anahtar üretin, `ISAKISI_BACKUP_KEY` olarak güvenli biçimde saklayın ve `uv run isakisi backup` ile şifreli yedek alın. Geri yüklemeden önce paneli ve işçiyi durdurun. `uv run isakisi restore yedek.enc --services-stopped` komutu veritabanını ve belge özetlerini doğrular, yeni giriş kodu üretir ve gönderimi manuel moda alır. Yedek tarihinden sonraki başvuruları işveren kayıtlarıyla karşılaştırıp belirsiz kayıtları çözdükten sonra `uv run isakisi resume-submission --verified-employer-history` kullanılabilir.

## Doğrulama

```bash
uv sync --frozen --group dev
uv run pytest tests/unit tests/integration
uv sync --frozen --group dev --extra browser
uv run playwright install chromium
uv run pytest tests/e2e
```

Model kullanımı isteğe bağlıdır. `LLM_BACKEND=ollama` yalnızca yerel Ollama'ya bağlanır. Ön yazı için doğrulanmış olgu kimlikleri seçer. Model erişimi veya çıktı doğrulaması başarısızsa şablon çalışır. Ollama'yı `OLLAMA_NO_CLOUD=1` ile başlatın ve yerel `/api/tags` yanıtındaki model özetini `OLLAMA_DIGEST` ile sabitleyin.

Sıralama ölçümü için `uv run isakisi benchmark degerlendirme.json` kullanın. Dosya, kaydedilmiş profil şemasıyla `profile` alanı ve her biri normalleştirilmiş `job` ile boolean `relevant` alanlarından oluşan `jobs` listesi içerir. En az 50 ilanı bağımsız olarak etiketleyin ve bunların en az 10'u ilgili olsun. `--e5` yerel model önbelleği, `semantic` eklentisi ve `ISAKISI_E5_REVISION` gerektirir. `--ollama` sabitlenmiş yerel modeli aynı veri kümesinde ölçer. Her yöntem için ilk 10'daki ilgili ilan oranı ve çalışma süresi raporlanır.

[Mimari belge](is-ilani-basvuru-mimarisi.md)
