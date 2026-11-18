import json
from time import perf_counter

import httpx

from .settings import Problem


def local_model(client, settings):
    if settings.llm_backend != "ollama" or not settings.ollama_digest:
        return False
    resp = client.get("http://127.0.0.1:11434/api/tags")
    resp.raise_for_status()
    return any(
        model["name"] == settings.ollama_model
        and model["digest"].removeprefix("sha256:") == settings.ollama_digest.removeprefix("sha256:")
        for model in resp.json()["models"]
    )


def model_json(client, settings, prompt, data, fields):
    response = client.post("http://127.0.0.1:11434/api/chat", json={
        "model": settings.ollama_model, "stream": False, "think": False,
        "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 256},
        "format": {"type": "object", "properties": fields, "required": list(fields)},
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
        ],
    })
    response.raise_for_status()
    return json.loads(response.json()["message"]["content"])


def model_data(job, candidates):
    return {"title": job["title"], "description": job["description"][:2500], "facts": candidates}


def select_facts(settings, job, facts):
    if settings.llm_backend != "ollama" or not facts:
        return None
    candidates = [{"id": f["id"], "text": f["text"][:400]} for f in facts[:20]]
    allowed = {f["id"] for f in candidates}
    try:
        with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
            if not local_model(client, settings):
                return None
            result = model_json(client, settings, "Select up to four relevant CV fact IDs. Return IDs only.",
                                model_data(job, candidates),
                                {"ids": {"type": "array", "maxItems": 4, "items": {"type": "string", "enum": sorted(allowed)}}})
        selection = result["ids"]
        if isinstance(selection, list) and 1 <= len(selection) <= 4 and all(isinstance(x, str) and x in allowed for x in selection):
            return set(selection)
    except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
        pass
    return None


def benchmark(fixtures, use_e5=False, cache_path=None, ollama_settings=None):
    from .matching import assess

    rows = fixtures["jobs"]
    profile = fixtures["profile"]
    if len(rows) < 50 or sum(r["relevant"] is True for r in rows) < 10:
        raise Problem("Ölçüm için en az 50 etiketli ilan ve 10 ilgili ilan gerekli.")
    start = perf_counter()
    ranked = sorted(rows, key=lambda row: (assess(row["job"], profile)["score"] or -1), reverse=True)
    output = {"rules": {"precision_at_10": sum(r["relevant"] for r in ranked[:10]) / 10,
                        "seconds": round(perf_counter() - start, 3)}}
    facts = [fact for fact in profile["facts"] if fact["verified_at"]]
    if use_e5:
        start = perf_counter()
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise Problem("E5 ölçümü için semantic eklentisini kurun.") from None
        import os
        from .db import atomic_write, digest, pack

        revision = os.getenv("ISAKISI_E5_REVISION")
        if not revision:
            raise Problem("E5 için ISAKISI_E5_REVISION ile model revizyonunu sabitleyin.")
        cache = {}
        if cache_path and cache_path.is_file():
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        model = SentenceTransformer("intfloat/multilingual-e5-small", revision=revision, device="cpu",
                                    local_files_only=True, trust_remote_code=False)
        query = "query: " + " ".join(f["text"] for f in facts)[:1600]
        passages = [[f"passage: {row['job']['title']} {row['job']['description'][i:i+800]}"
                     for i in range(0, max(1, len(row["job"]["description"])), 800)] for row in rows]
        texts = list(dict.fromkeys([query, *(text for job in passages for text in job)]))
        missing = [text for text in texts if digest([revision, text]) not in cache]
        if missing:
            for text, vector in zip(missing, model.encode(missing, normalize_embeddings=True).tolist(), strict=True):
                cache[digest([revision, text])] = vector
            if cache_path:
                atomic_write(cache_path, pack(cache).encode())
        q = cache[digest([revision, query])]
        similarities = [max(sum(a * b for a, b in zip(q, cache[digest([revision, p])], strict=True)) for p in job) for job in passages]
        top = sorted(range(len(rows)), key=lambda i: similarities[i], reverse=True)[:10]
        output["e5"] = {"precision_at_10": sum(rows[i]["relevant"] for i in top) / 10,
                        "seconds": round(perf_counter() - start, 3)}
    if ollama_settings:
        start = perf_counter()
        candidates = [fact["text"][:250] for fact in facts[:15]]
        results = []
        with httpx.Client(timeout=20, trust_env=False, follow_redirects=False) as client:
            try:
                if not local_model(client, ollama_settings):
                    raise Problem("Ollama model adı ve özetini doğrulayın.")
                for row in rows:
                    result = model_json(client, ollama_settings,
                                        "Score the fit from 0 to 100 using only the CV facts and job data.",
                                        model_data(row["job"], candidates),
                                        {"score": {"type": "integer", "minimum": 0, "maximum": 100}})
                    score = result["score"]
                    if type(score) is not int or not 0 <= score <= 100:
                        raise ValueError
                    results.append((score, row))
            except Problem:
                raise
            except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
                raise Problem("Yerel model tüm ilanlar için geçerli puan üretmedi.") from None
        top = sorted(results, key=lambda pair: pair[0], reverse=True)[:10]
        output["ollama"] = {"precision_at_10": sum(row["relevant"] for _, row in top) / 10,
                            "seconds": round(perf_counter() - start, 3)}
    return output
