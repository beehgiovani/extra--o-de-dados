"""Servidor local para conferência dos cadastros imobiliários de Mogi.

A aplicação escuta somente em 127.0.0.1. Ela cria um índice de consulta a
partir do checkpoint e entrega ao navegador apenas a página solicitada.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sqlite3
import threading
import unicodedata
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parent
DEFAULT_CURRENT_DIR = ROOT / "data" / "atual"
STATIC_DIR = ROOT / "interface_mogi"
CADASTRO_SOURCE = "mogi_cadastro_imobiliario_publico"


def normalized_text(value: Any) -> str:
    """Padroniza caixa, acentos e separadores para a pesquisa textual."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def normalized_identifier(value: Any) -> str:
    """Retém somente os dígitos de cadastro_id ou id_local para formar uma chave estável."""
    return re.sub(r"\D", "", str(value or ""))


def list_values(value: Any) -> List[str]:
    """Converte campos simples ou multivalorados em uma lista textual."""
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    return [str(value)]


def optional_float(value: Any) -> Optional[float]:
    """Converte um número sem preencher campos realmente ausentes."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class MogiDataRepository:
    """Consulta os 233 mil cadastros por um índice local derivado e regenerável."""

    def __init__(self, current_dir: Path) -> None:
        """Abre o checkpoint e reconstrói o índice quando a coleta foi atualizada."""
        self.current_dir = current_dir.resolve()
        self.checkpoint_path = self.current_dir / "checkpoint.sqlite"
        self.groups_path = self.current_dir / "resultados" / "mogi" / "locais_agrupados.json"
        self.index_path = self.current_dir / "interface_mogi.sqlite"
        if not self.checkpoint_path.is_file():
            raise FileNotFoundError(f"Checkpoint não encontrado: {self.checkpoint_path}")
        if not self.groups_path.is_file():
            raise FileNotFoundError(f"Agrupamentos territoriais não encontrados: {self.groups_path}")

        self.lock = threading.RLock()
        self.groups = self._load_groups()
        if self._index_is_outdated():
            self._rebuild_index()
        self.index = sqlite3.connect(self.index_path, check_same_thread=False)
        self.index.row_factory = sqlite3.Row
        source_uri = f"file:{self.checkpoint_path.as_posix()}?mode=ro"
        self.source = sqlite3.connect(source_uri, uri=True, check_same_thread=False)
        self.source.row_factory = sqlite3.Row
        self.summary = self._build_summary()
        self.options = self._build_options()

    def _load_groups(self) -> Dict[str, Dict[str, Any]]:
        """Carrega apenas os grupos usados para contexto espacial compartilhado."""
        with self.groups_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, list):
            raise ValueError("locais_agrupados.json deve conter uma lista.")
        return {
            normalized_identifier(item.get("id_local")): item
            for item in payload
            if normalized_identifier(item.get("id_local"))
        }

    def _source_signature(self) -> str:
        """Combina tamanho e data das entradas que alimentam o índice."""
        parts = []
        for path in (self.checkpoint_path, self.groups_path):
            stat = path.stat()
            parts.append(f"{stat.st_size}:{stat.st_mtime_ns}")
        return "|".join(parts)

    def _index_is_outdated(self) -> bool:
        """Verifica se o índice pode ser reutilizado sem reler toda a coleta."""
        if not self.index_path.is_file():
            return True
        try:
            connection = sqlite3.connect(self.index_path)
            row = connection.execute("SELECT value FROM metadata WHERE key='source_signature'").fetchone()
            connection.close()
            return not row or row[0] != self._source_signature()
        except sqlite3.DatabaseError:
            return True

    def _rebuild_index(self) -> None:
        """Cria um índice temporário e o publica somente quando estiver completo."""
        temporary_path = self.index_path.with_suffix(".sqlite.tmp")
        if temporary_path.exists():
            temporary_path.unlink()
        target = sqlite3.connect(temporary_path)
        target.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            CREATE TABLE cadastros (
                cadastro_id_normalizado TEXT PRIMARY KEY,
                cadastro_id TEXT NOT NULL,
                id_local_normalizado TEXT,
                id_local TEXT,
                logradouro TEXT,
                numero TEXT,
                complemento TEXT,
                tipo TEXT,
                uso_imovel TEXT,
                bairro TEXT,
                zona TEXT,
                situacao_valor TEXT,
                precisao TEXT,
                contexto_status TEXT,
                valor_venal REAL,
                exercicio_valor INTEGER,
                longitude REAL,
                latitude REAL,
                search_text TEXT NOT NULL
            );
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        source_uri = f"file:{self.checkpoint_path.as_posix()}?mode=ro"
        source = sqlite3.connect(source_uri, uri=True)
        source.row_factory = sqlite3.Row
        batch = []
        for row in source.execute(
            "SELECT properties_json, geometry_json FROM records WHERE source=?",
            (CADASTRO_SOURCE,),
        ):
            properties = json.loads(row["properties_json"])
            cadastro_key = normalized_identifier(properties.get("cadastro_id"))
            if not cadastro_key:
                continue
            local_key = normalized_identifier(properties.get("id_local"))
            group = self.groups.get(local_key, {})
            bairro = "; ".join(list_values(group.get("bairro_contexto")))
            zona = "; ".join(list_values(group.get("zoneamento_codigos")))
            geometry = json.loads(row["geometry_json"]) if row["geometry_json"] else None
            coordinates = geometry.get("coordinates") if geometry and geometry.get("type") == "Point" else None
            searchable = normalized_text(
                " ".join(
                    str(value or "")
                    for value in (
                        properties.get("cadastro_id"), properties.get("id_local"),
                        properties.get("logradouro"), properties.get("numero_imovel_quadra"),
                        properties.get("complemento"), properties.get("tipo"),
                        properties.get("uso_imovel"), bairro, zona,
                    )
                )
            )
            batch.append(
                (
                    cadastro_key, properties.get("cadastro_id"), local_key,
                    properties.get("id_local"), properties.get("logradouro"),
                    properties.get("numero_imovel_quadra"), properties.get("complemento"),
                    properties.get("tipo"), properties.get("uso_imovel"), bairro, zona,
                    properties.get("situacao_valor_venal"), properties.get("precisao_geometria"),
                    group.get("contexto_territorial_status"), optional_float(properties.get("valor_venal")),
                    properties.get("exercicio_valor_venal"), coordinates[0] if coordinates else None,
                    coordinates[1] if coordinates else None, searchable,
                )
            )
            if len(batch) >= 3000:
                target.executemany("INSERT OR REPLACE INTO cadastros VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
                batch.clear()
        if batch:
            target.executemany("INSERT OR REPLACE INTO cadastros VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
        source.close()
        target.executescript(
            """
            CREATE INDEX idx_interface_local ON cadastros(id_local_normalizado);
            CREATE INDEX idx_interface_bairro ON cadastros(bairro);
            CREATE INDEX idx_interface_zona ON cadastros(zona);
            CREATE INDEX idx_interface_situacao ON cadastros(situacao_valor);
            CREATE INDEX idx_interface_precisao ON cadastros(precisao);
            """
        )
        target.execute("INSERT INTO metadata(key,value) VALUES('source_signature',?)", (self._source_signature(),))
        target.commit()
        target.close()
        temporary_path.replace(self.index_path)

    def _build_summary(self) -> Dict[str, int]:
        """Calcula indicadores somente sobre os cadastros individuais."""
        expressions = {
            "cadastros": "COUNT(*)",
            "ids_locais": "COUNT(DISTINCT id_local_normalizado)",
            "com_valor_venal": "SUM(valor_venal IS NOT NULL)",
            "sem_valor_venal": "SUM(valor_venal IS NULL)",
            "georreferenciados": "SUM(longitude IS NOT NULL AND latitude IS NOT NULL)",
            "com_contexto_territorial": "SUM(contexto_status='cruzado_com_camadas_publicas')",
        }
        return {
            name: int(self.index.execute(f"SELECT {expression} FROM cadastros").fetchone()[0] or 0)
            for name, expression in expressions.items()
        }

    def _build_options(self) -> Dict[str, List[str]]:
        """Monta filtros somente com valores presentes no índice."""
        columns = {
            "bairros": "bairro", "zonas": "zona", "situacoes": "situacao_valor",
            "precisoes": "precisao", "contextos": "contexto_status",
        }
        result = {}
        for name, column in columns.items():
            rows = self.index.execute(
                f"SELECT DISTINCT {column} FROM cadastros WHERE {column} IS NOT NULL AND {column}<>'' ORDER BY {column}"
            )
            result[name] = [row[0] for row in rows]
        return result

    def search(self, params: Dict[str, List[str]]) -> Dict[str, Any]:
        """Pesquisa cadastro_id, id_local, endereço e classificações com paginação."""
        where, values = self._search_conditions(params)
        try:
            page = max(1, int(params.get("pagina", ["1"])[0]))
            limit = min(100, max(10, int(params.get("limite", ["30"])[0])))
        except ValueError:
            page, limit = 1, 30
        with self.lock:
            total = int(self.index.execute(f"SELECT COUNT(*) FROM cadastros WHERE {where}", values).fetchone()[0])
            rows = self.index.execute(
                f"SELECT * FROM cadastros WHERE {where} ORDER BY cadastro_id_normalizado LIMIT ? OFFSET ?",
                [*values, limit, (page - 1) * limit],
            ).fetchall()
        return {
            "total": total,
            "pagina": page,
            "limite": limit,
            "paginas": max(1, (total + limit - 1) // limit),
            "resultados": [self._list_item(row) for row in rows],
        }

    def _search_conditions(self, params: Dict[str, List[str]]) -> tuple[str, List[Any]]:
        """Traduz a mesma busca em filtros reutilizáveis pela lista e pelo mapa."""
        clauses = ["1=1"]
        values: List[Any] = []
        raw_query = params.get("q", [""])[0].strip()
        query_digits = normalized_identifier(raw_query)
        if re.fullmatch(r"\d{2}-\d{6}-\d{3}", raw_query):
            clauses.append("id_local_normalizado=?")
            values.append(query_digits)
        elif len(query_digits) == 6 and normalized_text(raw_query).replace(" ", "").isdigit():
            clauses.append("cadastro_id_normalizado=?")
            values.append(query_digits)
        else:
            for term in normalized_text(raw_query).split():
                clauses.append("search_text LIKE ?")
                values.append(f"%{term}%")
        filters = {
            "bairro": params.get("bairro", [""])[0],
            "zona": params.get("zona", [""])[0],
            "situacao_valor": params.get("situacao", [""])[0],
            "precisao": params.get("precisao", [""])[0],
            "contexto_status": params.get("contexto", [""])[0],
            "id_local_normalizado": normalized_identifier(params.get("id_local", [""])[0]),
        }
        for column, selected in filters.items():
            if selected:
                clauses.append(f"{column}=?")
                values.append(selected)
        return " AND ".join(clauses), values

    def map_points(self, params: Dict[str, List[str]]) -> Dict[str, Any]:
        """Agrupa todos os cadastros filtrados que usam a mesma referência espacial."""
        where, values = self._search_conditions(params)
        with self.lock:
            rows = self.index.execute(
                f"""SELECT longitude, latitude,
                           COUNT(*) AS cadastros,
                           COUNT(DISTINCT id_local_normalizado) AS ids_locais,
                           MIN(cadastro_id) AS cadastro_exemplo,
                           MIN(cadastro_id_normalizado) AS cadastro_exemplo_normalizado,
                           MIN(id_local) AS id_local_exemplo,
                           MIN(logradouro) AS logradouro_exemplo,
                           MIN(bairro) AS bairro_exemplo,
                           MIN(zona) AS zona_exemplo,
                           MIN(precisao) AS precisao_exemplo
                    FROM cadastros
                    WHERE {where} AND longitude IS NOT NULL AND latitude IS NOT NULL
                    GROUP BY longitude, latitude
                    ORDER BY cadastros DESC, latitude, longitude""",
                values,
            ).fetchall()
        points = [dict(row) for row in rows]
        return {
            "total_pontos": len(points),
            "cadastros_georreferenciados": sum(int(point["cadastros"]) for point in points),
            "pontos": points,
        }

    @staticmethod
    def _list_item(row: sqlite3.Row) -> Dict[str, Any]:
        """Converte uma linha do índice na ficha curta da lista e do mapa."""
        return {
            "cadastro_id": row["cadastro_id"],
            "cadastro_id_normalizado": row["cadastro_id_normalizado"],
            "id_local": row["id_local"],
            "logradouro": row["logradouro"],
            "numero": row["numero"],
            "complemento": row["complemento"],
            "tipo": row["tipo"],
            "uso_imovel": row["uso_imovel"],
            "bairro_contexto": list_values(row["bairro"]),
            "zoneamento_codigos": list_values(row["zona"]),
            "valor_venal": row["valor_venal"],
            "exercicio_valor_venal": row["exercicio_valor"],
            "situacao_valor_venal": row["situacao_valor"],
            "precisao_geometria": row["precisao"],
            "contexto_territorial_status": row["contexto_status"],
            "coordinates": [row["longitude"], row["latitude"]] if row["longitude"] is not None else None,
        }

    def detail(self, cadastro_id: str) -> Optional[Dict[str, Any]]:
        """Recupera o cadastro original, o contexto do id_local e o histórico venal correto."""
        key = normalized_identifier(cadastro_id)
        with self.lock:
            row = self.source.execute(
                "SELECT properties_json, geometry_json FROM records WHERE source=? AND record_id=?",
                (CADASTRO_SOURCE, f"cadastro:{key}"),
            ).fetchone()
            history = self.source.execute(
                """SELECT cadastro_id, id_local, exercicio, valor_venal_terreno,
                          valor_venal_construcao, valor_venal_total, tipo_fonte, fonte_recurso
                   FROM mogi_cadastro_valor_venal WHERE cadastro_id_normalizado=?
                   ORDER BY exercicio DESC""",
                (key,),
            ).fetchall()
        if not row:
            return None
        properties = json.loads(row["properties_json"])
        local_key = normalized_identifier(properties.get("id_local"))
        group = self.groups.get(local_key, {})
        for field in (
            "bairro_contexto", "distrito_contexto", "macrozona_siglas", "macrozona_contexto",
            "zoneamento_codigos", "zoneamento_descricoes", "zoneamento_detalhes",
            "contexto_territorial_status", "contexto_territorial_baseado_em", "fonte_contexto_territorial",
        ):
            properties[field] = group.get(field)
        if row["geometry_json"]:
            properties["geometry"] = json.loads(row["geometry_json"])
        return {
            "imovel": properties,
            "historico_valor_venal": [dict(item) for item in history],
            "grupo_local": {
                "quantidade_cadastros_no_local": group.get("quantidade_cadastros_no_local"),
                "quantidade_cadastros_ativos": group.get("quantidade_cadastros_ativos"),
            },
        }

    def close(self) -> None:
        """Fecha as conexões quando o servidor é encerrado."""
        self.index.close()
        self.source.close()


class MogiRequestHandler(BaseHTTPRequestHandler):
    """Atende a interface estática e a API de leitura local."""

    repository: MogiDataRepository

    def log_message(self, fmt: str, *args: Any) -> None:
        """Mantém no terminal um registro curto de cada consulta."""
        print(f"[interface] {self.address_string()} - {fmt % args}")

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        """Envia JSON UTF-8 sem cache de uma ficha que pode ser atualizada."""
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, request_path: str) -> None:
        """Serve somente arquivos localizados dentro da pasta da interface."""
        relative = "index.html" if request_path in ("", "/") else unquote(request_path.lstrip("/"))
        candidate = (STATIC_DIR / relative).resolve()
        if STATIC_DIR.resolve() not in candidate.parents and candidate != STATIC_DIR.resolve():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = candidate.read_bytes()
        media_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{media_type}; charset=utf-8" if media_type.startswith("text/") else media_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - nome definido pela biblioteca padrão
        """Roteia as consultas de leitura e rejeita caminhos inexistentes."""
        parsed = urlparse(self.path)
        if parsed.path == "/api/resumo":
            self._send_json({"resumo": self.repository.summary, "opcoes": self.repository.options})
        elif parsed.path == "/api/pontos":
            self._send_json(self.repository.map_points(parse_qs(parsed.query)))
        elif parsed.path == "/api/imoveis":
            self._send_json(self.repository.search(parse_qs(parsed.query)))
        elif parsed.path.startswith("/api/imoveis/"):
            detail = self.repository.detail(parsed.path.rsplit("/", 1)[-1])
            status = HTTPStatus.OK if detail else HTTPStatus.NOT_FOUND
            self._send_json(detail if detail else {"erro": "Cadastro não encontrado."}, status)
        else:
            self._send_static(parsed.path)


def parse_args() -> argparse.Namespace:
    """Define o diretório da coleta, a porta e a abertura do navegador."""
    parser = argparse.ArgumentParser(description="Abre a conferência local dos cadastros imobiliários de Mogi.")
    parser.add_argument("--current-dir", type=Path, default=DEFAULT_CURRENT_DIR, help="Diretório data/atual da coleta.")
    parser.add_argument("--port", type=int, default=8765, help="Porta local da interface (padrão: 8765).")
    parser.add_argument("--no-browser", action="store_true", help="Não abre o navegador automaticamente.")
    return parser.parse_args()


def main() -> int:
    """Carrega o índice, inicia o servidor local e abre a tela."""
    args = parse_args()
    repository = MogiDataRepository(args.current_dir)
    MogiRequestHandler.repository = repository
    server = ThreadingHTTPServer(("127.0.0.1", args.port), MogiRequestHandler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Base carregada: {repository.summary['cadastros']} cadastros individuais em {repository.summary['ids_locais']} ids locais.")
    print(f"Interface local: {url}")
    print("Use Ctrl+C neste terminal para encerrar.")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nInterface encerrada.")
    finally:
        server.server_close()
        repository.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
