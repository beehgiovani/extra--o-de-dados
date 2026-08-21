#!/usr/bin/env python3
"""
Extrator Imobiliário — cadastro territorial público
====================================================

Finalidade principal:
  Coletar, normalizar e separar dados públicos de cadastro imobiliário municipal
  para consulta territorial e apoio à análise de imóveis.

  Em Mogi, cadastro_id identifica a linha cadastral individual e id_local é
  preservado como referência territorial compartilhável. O programa não atribui
  significado jurídico aos trechos numéricos do id_local sem documentação oficial.

Cidades suportadas:
  - Itanhaém: camada vetorial pública (TileJSON/PBF) de arruamento, bairros,
    loteamentos e restrições urbanísticas.
  - Mogi das Cruzes: cadastro imobiliário público (CKAN), quadras e demais
    camadas territoriais do GeoMogi (bairro, zoneamento, macrozona, etc.).

Saídas geradas:
  - SQLite de checkpoint (retomável sem reprocessamento).
  - JSON consolidado por cadastro individual.
  - GeoJSON para QGIS, ArcGIS, Mapbox e similares.
  - CSV tabular para planilhas e integrações locais.
  - Valor venal público da base de IPTU de Mogi, vinculado por cadastro_id.
  - Importação local de certidões já obtidas, sem automação de portais.

Dependências:
  pip install requests mapbox-vector-tile
"""

from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import json
import logging
import math
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timezone
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import mapbox_vector_tile
except ImportError:  # só necessário no modo Itanhaém
    mapbox_vector_tile = None


APP_NAME = "ExtratorImobiliarioMestre/1.1"

ITANHAEM_TILEJSON = (
    "https://www2.itanhaem.sp.gov.br/ortofoto2012/mapa/"
    "tileserver.php?/arruamento.json"
)

MOGI_PACKAGE_API = (
    "https://dados.mogidascruzes.sp.gov.br/api/3/action/"
    "package_show?id=cadastro-imobiliario"
)

MOGI_QUADRAS_API = "https://geomogi.mogidascruzes.sp.gov.br/mapa/quadra"
MOGI_LOGRADOUROS_API = "https://geomogi.mogidascruzes.sp.gov.br/mapa/logradouro"


# -------------------------- Camada de persistência --------------------------

class CheckpointStore:
    """Mantém o estado da coleta em SQLite para que a execução possa ser retomada."""

    def __init__(self, path: Path) -> None:
        """Abre o banco local e prepara as tabelas usadas durante a coleta."""
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()

    def _create_schema(self) -> None:
        """Cria as tabelas e índices necessários sem alterar registros já coletados."""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS resources (
                source TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                status TEXT NOT NULL,
                http_status INTEGER,
                error TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source, resource_id)
            );

            CREATE TABLE IF NOT EXISTS records (
                source TEXT NOT NULL,
                record_id TEXT NOT NULL,
                properties_json TEXT NOT NULL,
                geometry_json TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source, record_id)
            );

            CREATE INDEX IF NOT EXISTS idx_records_source ON records(source);

            CREATE TABLE IF NOT EXISTS venal_validations (
                cidade TEXT NOT NULL,
                inscricao_normalizada TEXT NOT NULL,
                inscricao_imobiliaria TEXT NOT NULL,
                exercicio INTEGER NOT NULL,
                valor_venal REAL NOT NULL,
                moeda TEXT NOT NULL DEFAULT 'BRL',
                numero_certidao TEXT,
                data_emissao TEXT,
                arquivo_origem TEXT NOT NULL,
                linha_origem INTEGER,
                imported_at TEXT NOT NULL,
                PRIMARY KEY (cidade, inscricao_normalizada, exercicio)
            );

            CREATE INDEX IF NOT EXISTS idx_venal_validations_inscricao
            ON venal_validations(cidade, inscricao_normalizada);

            CREATE TABLE IF NOT EXISTS fiscal_status (
                cidade TEXT NOT NULL,
                inscricao_normalizada TEXT NOT NULL,
                inscricao_imobiliaria TEXT NOT NULL,
                competencia TEXT NOT NULL,
                possui_pendencia INTEGER NOT NULL,
                status_pendencia TEXT NOT NULL,
                valor_total_pendencia REAL,
                quantidade_pendencias INTEGER,
                fonte_consulta TEXT,
                data_consulta TEXT,
                numero_documento TEXT,
                arquivo_origem TEXT NOT NULL,
                linha_origem INTEGER,
                imported_at TEXT NOT NULL,
                PRIMARY KEY (cidade, inscricao_normalizada, competencia)
            );

            CREATE INDEX IF NOT EXISTS idx_fiscal_status_inscricao
            ON fiscal_status(cidade, inscricao_normalizada);

            CREATE TABLE IF NOT EXISTS mogi_iptu_venal (
                inscricao_normalizada TEXT NOT NULL,
                inscricao_imobiliaria TEXT NOT NULL,
                exercicio INTEGER NOT NULL,
                valor_venal_terreno REAL NOT NULL,
                valor_venal_construcao REAL NOT NULL,
                valor_venal_total REAL NOT NULL,
                tipo_fonte TEXT NOT NULL DEFAULT 'iptu',
                fonte_recurso TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                PRIMARY KEY (inscricao_normalizada, exercicio)
            );

            CREATE INDEX IF NOT EXISTS idx_mogi_iptu_venal_inscricao
            ON mogi_iptu_venal(inscricao_normalizada);

            CREATE TABLE IF NOT EXISTS mogi_cadastro_valor_venal (
                cadastro_id_normalizado TEXT NOT NULL,
                cadastro_id TEXT NOT NULL,
                id_local TEXT,
                exercicio INTEGER NOT NULL,
                valor_venal_terreno REAL NOT NULL,
                valor_venal_construcao REAL NOT NULL,
                valor_venal_total REAL NOT NULL,
                tipo_fonte TEXT NOT NULL DEFAULT 'iptu',
                fonte_recurso TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                PRIMARY KEY (cadastro_id_normalizado, exercicio)
            );

            CREATE INDEX IF NOT EXISTS idx_mogi_cadastro_venal_local
            ON mogi_cadastro_valor_venal(id_local, exercicio);
            """
        )
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(mogi_iptu_venal)")}
        if "tipo_fonte" not in columns:
            self.conn.execute(
                "ALTER TABLE mogi_iptu_venal ADD COLUMN tipo_fonte TEXT NOT NULL DEFAULT 'iptu'"
            )
        self.conn.commit()

    def clear_source_records(self, source: str) -> int:
        """Remove somente uma fonte regenerável antes de uma reimportação integral."""
        changes_before = self.conn.total_changes
        self.conn.execute("DELETE FROM records WHERE source=?", (source,))
        self.conn.commit()
        return self.conn.total_changes - changes_before

    def clear_mogi_cadastro_venal(self) -> int:
        """Esvazia a tabela venal corrigida para reprocessar todos os exercícios pela chave cadastral."""
        changes_before = self.conn.total_changes
        self.conn.execute("DELETE FROM mogi_cadastro_valor_venal")
        self.conn.commit()
        return self.conn.total_changes - changes_before

    @staticmethod
    def now() -> str:
        """Gera o horário UTC usado nos campos de auditoria do banco local."""
        return datetime.now(timezone.utc).isoformat()

    def set_state(self, key: str, value: Any) -> None:
        """Salva um marcador de execução que pode ser consultado em uma retomada."""
        self.conn.execute(
            """
            INSERT INTO state(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, json.dumps(value, ensure_ascii=False), self.now()),
        )
        self.conn.commit()

    def get_state(self, key: str, default: Any = None) -> Any:
        """Recupera um marcador salvo; devolve o valor padrão quando ele ainda não existe."""
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def resource_done(self, source: str, resource_id: str) -> bool:
        """Indica se um recurso de uma fonte já foi processado com sucesso."""
        row = self.conn.execute(
            "SELECT status FROM resources WHERE source=? AND resource_id=?",
            (source, resource_id),
        ).fetchone()
        return bool(row and row["status"] == "done")

    def mark_resource(
        self,
        source: str,
        resource_id: str,
        status: str,
        http_status: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        """Registra o resultado de uma tentativa de leitura de recurso externo."""
        self.conn.execute(
            """
            INSERT INTO resources(source, resource_id, status, http_status, error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, resource_id) DO UPDATE SET
                status=excluded.status, http_status=excluded.http_status,
                error=excluded.error, updated_at=excluded.updated_at
            """,
            (source, resource_id, status, http_status, error, self.now()),
        )
        self.conn.commit()

    def upsert_records(self, source: str, rows: Iterable[Dict[str, Any]]) -> int:
        """Grava ou atualiza um lote de feições normalizadas sem duplicar a chave da fonte."""
        prepared = []
        stamp = self.now()
        for row in rows:
            props = row["properties"]
            record_id = str(row["record_id"])
            prepared.append(
                (
                    source,
                    record_id,
                    json.dumps(props, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(row.get("geometry"), ensure_ascii=False, separators=(",", ":"))
                    if row.get("geometry") is not None
                    else None,
                    stamp,
                )
            )
        if not prepared:
            return 0
        self.conn.executemany(
            """
            INSERT INTO records(source, record_id, properties_json, geometry_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source, record_id) DO UPDATE SET
                properties_json=excluded.properties_json,
                geometry_json=excluded.geometry_json,
                updated_at=excluded.updated_at
            """,
            prepared,
        )
        self.conn.commit()
        return len(prepared)

    def iter_records(self, source: Optional[str] = None) -> Iterator[sqlite3.Row]:
        """Percorre os registros guardados, opcionalmente filtrando por uma fonte."""
        sql = "SELECT source, record_id, properties_json, geometry_json, updated_at FROM records"
        args: Tuple[Any, ...] = ()
        if source:
            sql += " WHERE source=?"
            args = (source,)
        sql += " ORDER BY source, record_id"
        yield from self.conn.execute(sql, args)

    def summary(self) -> Dict[str, int]:
        """Resume a quantidade de registros disponíveis em cada fonte coletada."""
        rows = self.conn.execute(
            "SELECT source, COUNT(*) AS n FROM records GROUP BY source ORDER BY source"
        ).fetchall()
        return {row["source"]: row["n"] for row in rows}

    def close(self) -> None:
        """Fecha a conexão com SQLite ao final da execução."""
        self.conn.close()

    def upsert_venal_validations(self, rows: Iterable[Dict[str, Any]]) -> int:
        """Armazena valores venais importados sem aceitar dados pessoais no modelo."""
        prepared = []
        stamp = self.now()
        for row in rows:
            prepared.append(
                (
                    row["cidade"],
                    row["inscricao_normalizada"],
                    row["inscricao_imobiliaria"],
                    row["exercicio"],
                    row["valor_venal"],
                    row.get("moeda", "BRL"),
                    row.get("numero_certidao"),
                    row.get("data_emissao"),
                    row["arquivo_origem"],
                    row.get("linha_origem"),
                    stamp,
                )
            )
        if not prepared:
            return 0
        self.conn.executemany(
            """
            INSERT INTO venal_validations(
                cidade, inscricao_normalizada, inscricao_imobiliaria, exercicio,
                valor_venal, moeda, numero_certidao, data_emissao, arquivo_origem,
                linha_origem, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cidade, inscricao_normalizada, exercicio) DO UPDATE SET
                inscricao_imobiliaria=excluded.inscricao_imobiliaria,
                valor_venal=excluded.valor_venal,
                moeda=excluded.moeda,
                numero_certidao=excluded.numero_certidao,
                data_emissao=excluded.data_emissao,
                arquivo_origem=excluded.arquivo_origem,
                linha_origem=excluded.linha_origem,
                imported_at=excluded.imported_at
            """,
            prepared,
        )
        self.conn.commit()
        return len(prepared)

    def iter_venal_validations(self) -> Iterator[sqlite3.Row]:
        """Percorre as validações venais importadas para vínculo e exportação."""
        yield from self.conn.execute(
            """
            SELECT cidade, inscricao_normalizada, inscricao_imobiliaria, exercicio,
                   valor_venal, moeda, numero_certidao, data_emissao, arquivo_origem,
                   linha_origem, imported_at
            FROM venal_validations
            ORDER BY cidade, inscricao_imobiliaria, exercicio
            """
        )

    def upsert_fiscal_status(self, rows: Iterable[Dict[str, Any]]) -> int:
        """Armazena a situação fiscal por inscrição sem guardar boleto ou dados pessoais."""
        prepared = []
        stamp = self.now()
        for row in rows:
            prepared.append(
                (
                    row["cidade"],
                    row["inscricao_normalizada"],
                    row["inscricao_imobiliaria"],
                    row["competencia"],
                    1 if row["possui_pendencia"] else 0,
                    row["status_pendencia"],
                    row.get("valor_total_pendencia"),
                    row.get("quantidade_pendencias"),
                    row.get("fonte_consulta"),
                    row.get("data_consulta"),
                    row.get("numero_documento"),
                    row["arquivo_origem"],
                    row.get("linha_origem"),
                    stamp,
                )
            )
        if not prepared:
            return 0
        self.conn.executemany(
            """
            INSERT INTO fiscal_status(
                cidade, inscricao_normalizada, inscricao_imobiliaria, competencia,
                possui_pendencia, status_pendencia, valor_total_pendencia,
                quantidade_pendencias, fonte_consulta, data_consulta,
                numero_documento, arquivo_origem, linha_origem, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cidade, inscricao_normalizada, competencia) DO UPDATE SET
                inscricao_imobiliaria=excluded.inscricao_imobiliaria,
                possui_pendencia=excluded.possui_pendencia,
                status_pendencia=excluded.status_pendencia,
                valor_total_pendencia=excluded.valor_total_pendencia,
                quantidade_pendencias=excluded.quantidade_pendencias,
                fonte_consulta=excluded.fonte_consulta,
                data_consulta=excluded.data_consulta,
                numero_documento=excluded.numero_documento,
                arquivo_origem=excluded.arquivo_origem,
                linha_origem=excluded.linha_origem,
                imported_at=excluded.imported_at
            """,
            prepared,
        )
        self.conn.commit()
        return len(prepared)

    def iter_fiscal_status(self) -> Iterator[sqlite3.Row]:
        """Percorre as situações fiscais importadas para vínculo e exportação."""
        yield from self.conn.execute(
            """
            SELECT cidade, inscricao_normalizada, inscricao_imobiliaria, competencia,
                   possui_pendencia, status_pendencia, valor_total_pendencia,
                   quantidade_pendencias, fonte_consulta, data_consulta,
                   numero_documento, arquivo_origem, linha_origem, imported_at
            FROM fiscal_status
            ORDER BY cidade, inscricao_imobiliaria, competencia
            """
        )

    def upsert_mogi_iptu_venal(
        self, rows: Iterable[Dict[str, Any]], preserve_existing: bool = False
    ) -> int:
        """Guarda valores venais por cadastro sem deixar ITBI substituir o IPTU do mesmo ano."""
        prepared = []
        stamp = self.now()
        for row in rows:
            prepared.append(
                (
                    row["cadastro_id_normalizado"],
                    row["cadastro_id"],
                    row.get("id_local"),
                    row["exercicio"],
                    row["valor_venal_terreno"],
                    row["valor_venal_construcao"],
                    row["valor_venal_total"],
                    row.get("tipo_fonte", "iptu"),
                    row["fonte_recurso"],
                    stamp,
                )
            )
        if not prepared:
            return 0
        changes_before = self.conn.total_changes
        conflict = (
            "ON CONFLICT(cadastro_id_normalizado, exercicio) DO NOTHING"
            if preserve_existing
            else """ON CONFLICT(cadastro_id_normalizado, exercicio) DO UPDATE SET
                cadastro_id=excluded.cadastro_id,
                id_local=excluded.id_local,
                valor_venal_terreno=excluded.valor_venal_terreno,
                valor_venal_construcao=excluded.valor_venal_construcao,
                valor_venal_total=excluded.valor_venal_total,
                tipo_fonte=excluded.tipo_fonte,
                fonte_recurso=excluded.fonte_recurso,
                imported_at=excluded.imported_at"""
        )
        self.conn.executemany(
            f"""
            INSERT INTO mogi_cadastro_valor_venal(
                cadastro_id_normalizado, cadastro_id, id_local, exercicio,
                valor_venal_terreno, valor_venal_construcao, valor_venal_total,
                tipo_fonte, fonte_recurso, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            {conflict}
            """,
            prepared,
        )
        self.conn.commit()
        return self.conn.total_changes - changes_before

    def iter_mogi_iptu_venal(self) -> Iterator[sqlite3.Row]:
        """Percorre os valores venais oficiais em ordem de cadastro e exercício."""
        yield from self.conn.execute(
            """
            SELECT cadastro_id_normalizado, cadastro_id, id_local, exercicio,
                   valor_venal_terreno, valor_venal_construcao, valor_venal_total,
                   tipo_fonte, fonte_recurso, imported_at
            FROM mogi_cadastro_valor_venal
            ORDER BY cadastro_id_normalizado, exercicio
            """
        )


# -------------------------- HTTP persistente --------------------------

class PersistentHttpClient:
    """Centraliza requisições públicas com limite de ritmo, tentativas e cookies locais."""

    def __init__(
        self,
        cookie_path: Path,
        timeout: int = 45,
        min_interval: float = 0.2,
    ) -> None:
        """Configura uma sessão HTTP previsível para as fontes públicas do projeto."""
        self.timeout = timeout
        self.min_interval = min_interval
        self._last_request = 0.0

        self.session = requests.Session()
        retry = Retry(
            total=5,
            connect=5,
            read=5,
            status=5,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update(
            {
                "User-Agent": APP_NAME,
                "Accept": "application/json,text/plain,*/*",
            }
        )

        self.cookie_path = cookie_path
        jar = MozillaCookieJar(str(cookie_path))
        if cookie_path.exists():
            try:
                jar.load(ignore_discard=True, ignore_expires=True)
            except Exception as exc:
                logging.warning("Não foi possível carregar cookies persistidos: %s", exc)
        self.session.cookies = jar

    def _wait(self) -> None:
        """Respeita o intervalo mínimo definido entre duas chamadas consecutivas."""
        remaining = self.min_interval - (time.monotonic() - self._last_request)
        if remaining > 0:
            time.sleep(remaining)

    def _save_cookies(self) -> None:
        """Persiste cookies técnicos para permitir a retomada da mesma sessão pública."""
        try:
            self.session.cookies.save(ignore_discard=True, ignore_expires=True)
        except Exception as exc:
            logging.warning("Não foi possível persistir cookies: %s", exc)

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Executa uma requisição aplicando timeout, limite de ritmo e persistência de cookies."""
        self._wait()
        kwargs.setdefault("timeout", self.timeout)
        response = self.session.request(method.upper(), url, **kwargs)
        self._last_request = time.monotonic()
        self._save_cookies()
        return response

    def get_json(self, url: str, **kwargs: Any) -> Dict[str, Any]:
        """Busca um endpoint público e valida que a resposta HTTP foi bem-sucedida."""
        response = self.request("GET", url, **kwargs)
        response.raise_for_status()
        return response.json()

# -------------------------- Funções geoespaciais --------------------------

def tile_range_for_bounds(bounds: Sequence[float], zoom: int) -> Tuple[int, int, int, int]:
    """Calcula a grade XYZ que cobre os limites geográficos informados."""
    west, south, east, north = map(float, bounds)
    n = 2 ** zoom

    def x_from_lon(lon: float) -> int:
        """Converte longitude em coluna XYZ, limitada à grade válida do zoom."""
        return max(0, min(n - 1, int((lon + 180.0) / 360.0 * n)))

    def y_from_lat(lat: float) -> int:
        """Converte latitude em linha XYZ, respeitando o limite do Web Mercator."""
        lat = max(min(lat, 85.05112878), -85.05112878)
        y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
        return max(0, min(n - 1, int(y)))

    xmin, xmax = sorted((x_from_lon(west), x_from_lon(east)))
    ymin, ymax = sorted((y_from_lat(north), y_from_lat(south)))
    return xmin, xmax, ymin, ymax


def tile_coord_to_lonlat(px: float, py: float, x: int, y: int, z: int, extent: int) -> List[float]:
    """Converte um ponto local de tile vetorial para longitude e latitude WGS84."""
    n = 2 ** z
    gx = (x + (px / extent)) / n
    gy = (y + (py / extent)) / n
    lon = gx * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * gy))))
    return [round(lon, 7), round(lat, 7)]


def transform_tile_coordinates(coords: Any, x: int, y: int, z: int, extent: int) -> Any:
    """Transforma coordenadas locais de PBF para coordenadas WGS84 GeoJSON."""
    if isinstance(coords, (list, tuple)):
        if len(coords) >= 2 and isinstance(coords[0], (int, float)) and isinstance(coords[1], (int, float)):
            return tile_coord_to_lonlat(float(coords[0]), float(coords[1]), x, y, z, extent)
        return [transform_tile_coordinates(c, x, y, z, extent) for c in coords]
    return coords


def safe_text(value: Any) -> Optional[str]:
    """Converte um valor textual em texto limpo ou retorna nulo quando ele está vazio."""
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def safe_float(value: Any) -> Optional[float]:
    """Converte números vindos de CSV, aceitando separadores brasileiros e decimais simples."""
    return parse_brl_number(value)


def geometry_bbox(geometry: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    """Calcula o envelope WGS84 de uma geometria GeoJSON sem dependências GIS extras."""
    coords = geometry.get("coordinates")
    points: List[Tuple[float, float]] = []

    def visit(value: Any) -> None:
        """Visita recursivamente os níveis de coordenadas de uma geometria GeoJSON."""
        if isinstance(value, (list, tuple)):
            if len(value) >= 2 and all(isinstance(v, (int, float)) for v in value[:2]):
                points.append((float(value[0]), float(value[1])))
            else:
                for child in value:
                    visit(child)

    visit(coords)
    if not points:
        return None
    xs, ys = zip(*points)
    return min(xs), min(ys), max(xs), max(ys)


def point_in_ring(point: Tuple[float, float], ring: Any) -> bool:
    """Testa um ponto contra um anel GeoJSON e considera a borda como pertencente ao anel."""
    if not isinstance(ring, (list, tuple)) or len(ring) < 3:
        return False
    x, y = point
    inside = False
    previous = ring[-1]
    for current in ring:
        if not (
            isinstance(previous, (list, tuple))
            and isinstance(current, (list, tuple))
            and len(previous) >= 2
            and len(current) >= 2
        ):
            previous = current
            continue
        x1, y1 = float(previous[0]), float(previous[1])
        x2, y2 = float(current[0]), float(current[1])
        cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
        if abs(cross) <= 1e-10 and min(x1, x2) - 1e-10 <= x <= max(x1, x2) + 1e-10 and min(y1, y2) - 1e-10 <= y <= max(y1, y2) + 1e-10:
            return True
        if (y1 > y) != (y2 > y):
            intersection_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < intersection_x:
                inside = not inside
        previous = current
    return inside


def point_in_geometry(point: Tuple[float, float], geometry: Dict[str, Any]) -> bool:
    """Testa um ponto em Polygon ou MultiPolygon, respeitando eventuais vazios internos."""
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    polygons = [coordinates] if geometry_type == "Polygon" else coordinates if geometry_type == "MultiPolygon" else []
    for polygon in polygons or []:
        if not polygon or not point_in_ring(point, polygon[0]):
            continue
        if not any(point_in_ring(point, hole) for hole in polygon[1:]):
            return True
    return False


def quadra_key_from_inscricao(inscricao: Any) -> Optional[str]:
    """Extrai a chave de quadra pública (ex: '01008') do id_local municipal.

    Mogi: SS-QQQQLL-UUU → chave = SS + QQQQ (zero-padded a 3 dígitos).
    Para 01-000801-001, a chave resultante é '01008'.
    """
    parsed = parse_inscricao_mogi(inscricao)
    return parsed["quadra_chave"] if parsed else None


def parse_inscricao_mogi(inscricao: Any) -> Optional[Dict[str, Any]]:
    """Decompõe o id_local apenas nos trechos necessários ao vínculo territorial.

    O portal não publica um dicionário que confirme a semântica de todos os
    dígitos. Por isso o sufixo final é preservado sem chamá-lo automaticamente
    de subunidade. A unidade cadastral individual é identificada por cadastro_id.
    """
    if not inscricao:
        return None
    parts = re.findall(r"\d+", str(inscricao))
    if len(parts) < 3 or len(parts[0]) != 2 or len(parts[1]) < 6:
        # Fallback para inscrições sem sublote explícito
        if len(parts) >= 2 and len(parts[0]) == 2 and len(parts[1]) >= 4:
            setor = parts[0]
            quadra_num = int(parts[1][:4])
            lote_num = int(parts[1][4:]) if len(parts[1]) > 4 else 0
            sufixo_local = 0
        else:
            return None
    else:
        setor = parts[0]
        quadra_num = int(parts[1][:4])
        lote_num = int(parts[1][4:]) if len(parts[1]) > 4 else 0
        sufixo_local = int(parts[2])
    return {
        "setor": setor,
        "quadra_num": quadra_num,
        "lote_num": lote_num,
        "sufixo_local": sufixo_local,
        "quadra_chave": f"{setor}{str(quadra_num).zfill(3)}",
        "lote_chave": f"{setor}{str(quadra_num).zfill(3)}{str(lote_num).zfill(2)}",
        "id_local_formatado": f"{setor}-{parts[1]}-{sufixo_local:03d}",
    }


def normalize_quadra_key(value: Any) -> Optional[str]:
    """Padroniza a chave de quadra pública em cinco dígitos para os cruzamentos do GeoMogi."""
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return None
    return digits.zfill(5)


def logradouro_key(value: Any) -> Optional[str]:
    """Normaliza nomes públicos de vias para associação sem sensibilidade a acento."""
    text = str(value or "").strip().upper()
    if not text:
        return None
    text = unicodedata.normalize("NFKD", text).encode("ASCII", "ignore").decode("ASCII")
    text = re.split(r"\s+-\s+\d{5}-?\d{3}\s*$", text)[0]
    text = re.sub(r"^(RUA|R)\s+", "R ", text)
    text = re.sub(r"^(AVENIDA|AV)\s+", "AV ", text)
    text = re.sub(r"^(ESTRADA|EST)\s+", "EST ", text)
    text = re.sub(r"^(RODOVIA|ROD)\s+", "ROD ", text)
    text = re.sub(r"^(PRACA|PC)\s+", "PC ", text)
    text = re.sub(r"^(TRAVESSA|TV)\s+", "TV ", text)
    text = re.sub(r"^(VIELA|VE)\s+", "VE ", text)
    text = re.sub(r"^(ROTATORIA|ROT)\s+", "ROT ", text)
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    text = re.sub(
        r"^(R|AV|EST|ROD|PC|TV|VE|ROT)\s+(DR|DOUTOR|DRA|DOUTORA|PRF|PRFA|PROF|PROFA|PROFESSOR|PROFESSORA|TTE|TEN|TENENTE|FR|FREI)\s+",
        r"\1 ",
        text,
    )
    return re.sub(r"\s+", " ", text).strip() or None


# -------------------------- Extrator de Itanhaém --------------------------

class ItanhaemPublicExtractor:
    """Coleta somente a camada pública de arruamento disponibilizada por Itanhaém."""

    source = "itanhaem_arruamento_publico"

    def __init__(self, client: PersistentHttpClient, store: CheckpointStore) -> None:
        """Recebe os serviços compartilhados de HTTP e checkpoint usados pela fonte."""
        self.client = client
        self.store = store

    def fetch_metadata(self) -> Dict[str, Any]:
        """Lê o TileJSON oficial e guarda seus metadados para auditoria da coleta."""
        metadata = self.client.get_json(ITANHAEM_TILEJSON)
        required = ("tiles", "bounds", "minzoom", "maxzoom")
        missing = [key for key in required if key not in metadata]
        if missing:
            raise ValueError(f"TileJSON incompleto: campos ausentes {missing}")
        self.store.set_state("itanhaem_tilejson", metadata)
        return metadata

    @staticmethod
    def feature_id(props: Dict[str, Any], geometry: Dict[str, Any]) -> str:
        """Usa o identificador da feição ou um hash estável quando a fonte não o fornece."""
        gid = props.get("gid")
        if gid is not None:
            return f"gid:{gid}"
        raw = json.dumps({"p": props, "g": geometry}, ensure_ascii=False, sort_keys=True)
        return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def process_tile(self, template: str, z: int, x: int, y: int) -> int:
        """Baixa um tile, converte sua geometria para WGS84 e registra as feições válidas."""
        resource_id = f"{z}/{x}/{y}"
        if self.store.resource_done(self.source, resource_id):
            return 0

        url = template.format(z=z, x=x, y=y)
        try:
            response = self.client.request("GET", url, headers={"Accept": "application/x-protobuf,*/*"})
            if response.status_code == 404:
                self.store.mark_resource(self.source, resource_id, "done", 404)
                return 0
            response.raise_for_status()
            if not response.content:
                self.store.mark_resource(self.source, resource_id, "done", response.status_code)
                return 0
            if mapbox_vector_tile is None:
                raise RuntimeError("Dependência ausente: execute 'pip install mapbox-vector-tile'.")

            decoded = mapbox_vector_tile.decode(response.content)
            records: List[Dict[str, Any]] = []
            for layer_name, layer in decoded.items():
                extent = int(layer.get("extent", 4096))
                for feature in layer.get("features", []):
                    props = feature.get("properties") or {}
                    geometry = feature.get("geometry")
                    if not geometry:
                        continue
                    geo = {
                        "type": geometry.get("type"),
                        "coordinates": transform_tile_coordinates(
                            geometry.get("coordinates"), x, y, z, extent
                        ),
                    }
                    normalized = {
                        "cidade": "Itanhaém",
                        "fonte": "TileServer público - camada arruamento",
                        "camada": layer_name,
                        "gid": props.get("gid"),
                        "tipo": safe_text(props.get("Tipo")),
                        "descricao": safe_text(props.get("Descrição") or props.get("Descriu00e7u00e3o")),
                        "bairro": safe_text(props.get("Bairro")),
                        "loteamento": safe_text(props.get("Loteamento")),
                        "lei_corredor": safe_text(props.get("Lei Corredor ")),
                        "restricao": safe_text(props.get("Restrição") or props.get("Restriu00e7u00e3o")),
                        "tile": {"z": z, "x": x, "y": y},
                    }
                    records.append(
                        {
                            "record_id": self.feature_id(props, geo),
                            "properties": normalized,
                            "geometry": geo,
                        }
                    )
            count = self.store.upsert_records(self.source, records)
            self.store.mark_resource(self.source, resource_id, "done", response.status_code)
            return count
        except Exception as exc:
            logging.exception("Falha no tile %s", resource_id)
            self.store.mark_resource(self.source, resource_id, "error", error=str(exc))
            return 0

    def run(
        self,
        limit_tiles: Optional[int] = None,
        force: bool = False,
        only_tile: Optional[Tuple[int, int, int]] = None,
    ) -> Dict[str, int]:
        """Percorre a grade oficial de tiles, permitindo retomada, recorte e reprocessamento."""
        metadata = self.fetch_metadata()
        template = metadata["tiles"][0]
        z = int(metadata["maxzoom"])
        xmin, xmax, ymin, ymax = tile_range_for_bounds(metadata["bounds"], z)
        total = (xmax - xmin + 1) * (ymax - ymin + 1)
        logging.info("Itanhaém: zoom=%s, grade=%s tiles, X=%s..%s, Y=%s..%s", z, total, xmin, xmax, ymin, ymax)

        if only_tile is not None:
            tz, tx, ty = only_tile
            if tz != z:
                raise ValueError(f"O TileJSON utiliza zoom máximo {z}; o tile informado possui zoom {tz}.")
            tiles: Iterable[Tuple[int, int, int]] = [(tz, tx, ty)]
            total = 1
        else:
            tiles = ((z, x, y) for y in range(ymin, ymax + 1) for x in range(xmin, xmax + 1))

        processed = 0
        inserted = 0
        for tz, x, y in tiles:
            if limit_tiles is not None and processed >= limit_tiles:
                self.store.set_state("itanhaem_last_run", {"limited": True, "processed": processed})
                return {"tiles_processados": processed, "registros_gravados": inserted}
            resource_id = f"{tz}/{x}/{y}"
            if force and self.store.resource_done(self.source, resource_id):
                self.store.mark_resource(self.source, resource_id, "pending")
            inserted += self.process_tile(template, tz, x, y)
            processed += 1
            if processed % 25 == 0:
                logging.info("Itanhaém: %s/%s tiles verificados; %s registros lidos", processed, total, inserted)

        self.store.set_state("itanhaem_last_run", {"limited": False, "processed": processed})
        return {"tiles_processados": processed, "registros_gravados": inserted}


# -------------------------- Extratores de Mogi --------------------------

# Camadas GeoMogi confirmadas (HTTP 200) úteis para contexto territorial:
#   quadra     – polígono da quadra fiscal (georreferência principal do lote)
#   logradouro – eixo da via (fallback de georreferência)
#   bairro     – delimitação de bairros (contexto de vizinhança)
#   zoneamento – zonas de uso e ocupação do solo (restrições urbanísticas)
#   macrozona  – macrozoneamento municipal (planejamento urbano)
#   limite     – limites administrativos do município
#   saude      – equipamentos de saúde (referência de entorno)
#   distrito   – divisão distrital do município
GEOMOGI_LAYERS: Dict[str, str] = {
    "quadra":      "Polígono da quadra fiscal — georreferência principal por lote",
    "logradouro":  "Eixo de logradouro público — fallback de georreferência",
    "bairro":      "Delimitação de bairros — contexto de vizinhança para avaliação",
    "zoneamento":  "Zonas de uso e ocupação do solo — restrições e potencial construtivo",
    "macrozona":   "Macrozoneamento municipal — planejamento urbano estratégico",
    "limite":      "Limites administrativos do município",
    "saude":       "Equipamentos de saúde — referência de entorno e infraestrutura",
    "distrito":    "Divisão distrital — referência territorial administrativa",
}

GEOMOGI_BASE = "https://geomogi.mogidascruzes.sp.gov.br/mapa"


class MogiGeoLayerExtractor:
    """Coleta qualquer camada pública do GeoMogi para contextualização territorial.

    Utilizado para bairro, zoneamento, macrozona, limite, saude e distrito.
    Cada camada acrescenta contexto urbanístico e infraestrutura de entorno.
    """

    def __init__(
        self,
        layer_name: str,
        client: PersistentHttpClient,
        store: CheckpointStore,
    ) -> None:
        """Inicializa o extrator para uma camada específica do GeoMogi."""
        if layer_name not in GEOMOGI_LAYERS:
            raise ValueError(f"Camada '{layer_name}' não está na lista de camadas confirmadas do GeoMogi.")
        self.layer_name = layer_name
        self.source = f"mogi_{layer_name}_geomogi"
        self.client = client
        self.store = store

    def run(self, force: bool = False) -> Dict[str, int]:
        """Baixa a camada GeoMogi, valida geometrias e grava no checkpoint.

        Registros sem geometria válida são descartados com log de depuração.
        A camada é usada para enriquecer o contexto territorial dos cadastros.
        """
        resource_id = f"geomogi/{self.layer_name}"
        if self.store.resource_done(self.source, resource_id) and not force:
            return {"registros_gravados": 0, "ignorados": 0}

        url = f"{GEOMOGI_BASE}/{self.layer_name}"
        response = self.client.request("GET", url, headers={"Accept": "application/json"})
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError(f"GeoMogi/{self.layer_name}: resposta inesperada (não é lista).")

        rows: List[Dict[str, Any]] = []
        ignored = 0
        descricao_camada = GEOMOGI_LAYERS[self.layer_name]
        for item in payload:
            try:
                geo_raw = item.get("poligono") or item.get("linha") or item.get("ponto")
                if not geo_raw:
                    raise ValueError("sem geometria")
                geometry = json.loads(geo_raw)
                if not geometry_bbox(geometry):
                    raise ValueError("geometria inválida ou vazia")
                texto = safe_text(item.get("texto") or item.get("nome") or item.get("descricao"))
                public_attributes: Dict[str, Any] = {}
                for raw_name, value in item.items():
                    field_name = normalized_field_name(raw_name)
                    if field_name in {"poligono", "linha", "ponto", "cor"} or value in (None, ""):
                        continue
                    if isinstance(value, (str, int, float, bool)):
                        public_attributes[field_name] = value
                rows.append({
                    "record_id": f"{self.layer_name}:{item.get('id', len(rows))}",
                    "properties": {
                        "cidade": "Mogi das Cruzes",
                        "fonte": f"GeoMogi — {descricao_camada}",
                        "camada": self.layer_name,
                        "texto": texto,
                        "id_fonte": item.get("id"),
                        "precisao_geometria": self.layer_name,
                        **public_attributes,
                    },
                    "geometry": geometry,
                })
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                ignored += 1
                logging.debug("GeoMogi/%s: item ignorado: %s", self.layer_name, exc)

        inserted = self.store.upsert_records(self.source, rows)
        self.store.mark_resource(self.source, resource_id, "done", response.status_code)
        self.store.set_state(
            f"mogi_{self.layer_name}_last_run",
            {"processados": len(payload), "gravados": inserted, "ignorados": ignored},
        )
        logging.info("GeoMogi/%s: %s gravados, %s ignorados.", self.layer_name, inserted, ignored)
        return {"registros_gravados": inserted, "ignorados": ignored}


class MogiQuadrasExtractor:
    """Coleta as quadras fiscais públicas do GeoMogi.

    A geometria de lote individual não é disponibilizada publicamente pela
    Prefeitura. A quadra é a menor unidade geométrica pública, usada como
    georreferência de contexto para cada inscrição imobiliária.
    """

    source = "mogi_quadras_publicas"

    def __init__(self, client: PersistentHttpClient, store: CheckpointStore) -> None:
        """Recebe os serviços compartilhados usados para coletar as quadras públicas."""
        self.client = client
        self.store = store


    def run(self, force: bool = False) -> Dict[str, int]:
        """Baixa e valida os polígonos públicos de quadra antes de gravá-los no checkpoint."""
        resource_id = "geomogi/quadra"
        if self.store.resource_done(self.source, resource_id) and not force:
            return {"quadras_processadas": 0, "registros_gravados": 0}

        response = self.client.request("GET", MOGI_QUADRAS_API, headers={"Accept": "application/json"})
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("A API pública do GeoMogi retornou uma resposta inesperada para quadras.")

        rows: List[Dict[str, Any]] = []
        ignored = 0
        for item in payload:
            try:
                geometry = json.loads(item["poligono"])
                if geometry.get("type") not in {"Polygon", "MultiPolygon"} or not geometry_bbox(geometry):
                    raise ValueError("geometria ausente ou inválida")
                quadra = normalize_quadra_key(item.get("texto"))
                if not quadra:
                    raise ValueError("código de quadra ausente")
                rows.append(
                    {
                        "record_id": f"quadra:{item['id']}",
                        "properties": {
                            "cidade": "Mogi das Cruzes",
                            "fonte": "GeoMogi - camada pública de quadras",
                            "camada": "quadra",
                            "quadra_chave": quadra,
                            "quadra_codigo_fonte": safe_text(item.get("texto")),
                            "quadra_id_fonte": item.get("id"),
                            "precisao_geometria": "quadra",
                        },
                        "geometry": geometry,
                    }
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                ignored += 1
                logging.debug("GeoMogi: quadra ignorada: %s", exc)

        inserted = self.store.upsert_records(self.source, rows)
        self.store.mark_resource(self.source, resource_id, "done", response.status_code)
        self.store.set_state(
            "mogi_quadras_last_run",
            {"processadas": len(payload), "gravadas": inserted, "ignoradas": ignored, "url": MOGI_QUADRAS_API},
        )
        return {"quadras_processadas": len(payload), "registros_gravados": inserted, "ignoradas": ignored}


class MogiLogradourosExtractor:
    """Coleta os eixos públicos de logradouro do GeoMogi para fallback territorial."""

    source = "mogi_logradouros_publicos"

    def __init__(self, client: PersistentHttpClient, store: CheckpointStore) -> None:
        """Recebe os serviços compartilhados usados para coletar os logradouros públicos."""
        self.client = client
        self.store = store

    def run(self, force: bool = False) -> Dict[str, int]:
        """Baixa os eixos de logradouro que servem como fallback de localização em Mogi."""
        resource_id = "geomogi/logradouro"
        if self.store.resource_done(self.source, resource_id) and not force:
            return {"logradouros_processados": 0, "registros_gravados": 0}

        response = self.client.request("GET", MOGI_LOGRADOUROS_API, headers={"Accept": "application/json"})
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("A API pública do GeoMogi retornou uma resposta inesperada para logradouros.")

        rows: List[Dict[str, Any]] = []
        ignored = 0
        for item in payload:
            try:
                geometry = json.loads(item["linha"])
                if geometry.get("type") not in {"LineString", "MultiLineString"} or not geometry_bbox(geometry):
                    raise ValueError("geometria ausente ou inválida")
                nome = safe_text(item.get("texto"))
                chave = logradouro_key(nome)
                if not chave:
                    raise ValueError("nome de logradouro ausente")
                rows.append(
                    {
                        "record_id": f"logradouro:{item['id']}",
                        "properties": {
                            "cidade": "Mogi das Cruzes",
                            "fonte": "GeoMogi - camada pública de logradouros",
                            "camada": "logradouro",
                            "logradouro": nome,
                            "logradouro_chave": chave,
                            "cep": safe_text(item.get("cep")),
                            "logradouro_id_fonte": item.get("id"),
                            "precisao_geometria": "eixo_do_logradouro",
                        },
                        "geometry": geometry,
                    }
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                ignored += 1
                logging.debug("GeoMogi: logradouro ignorado: %s", exc)

        inserted = self.store.upsert_records(self.source, rows)
        self.store.mark_resource(self.source, resource_id, "done", response.status_code)
        self.store.set_state(
            "mogi_logradouros_last_run",
            {"processados": len(payload), "gravados": inserted, "ignorados": ignored, "url": MOGI_LOGRADOUROS_API},
        )
        return {"logradouros_processados": len(payload), "registros_gravados": inserted, "ignoradas": ignored}


class MogiPublicExtractor:
    """Coleta e normaliza o cadastro imobiliário publicado no portal de dados abertos."""

    source = "mogi_cadastro_imobiliario_publico"

    def __init__(self, client: PersistentHttpClient, store: CheckpointStore) -> None:
        """Recebe os serviços compartilhados usados para baixar os CSVs públicos de Mogi."""
        self.client = client
        self.store = store

    def find_resources(self, year: int, full: bool = False) -> List[Dict[str, Any]]:
        """Descobre no CKAN os arquivos do exercício, sem depender de URLs fixas no código."""
        response = self.client.get_json(MOGI_PACKAGE_API)
        if not response.get("success"):
            raise RuntimeError("A API CKAN retornou success=false")
        resources = response["result"]["resources"]

        if full:
            pattern = re.compile(rf"^pmmc_imobiliario_{year}_part(\d+)\.csv$", re.IGNORECASE)
            parts = []
            for resource in resources:
                match = pattern.match(str(resource.get("name", "")))
                if match and str(resource.get("format", "")).upper() == "CSV":
                    parts.append((int(match.group(1)), resource))
            if not parts:
                raise LookupError(f"Nenhuma parte pmmc_imobiliario_{year}_partN.csv encontrada na API CKAN.")
            parts.sort(key=lambda item: item[0])
            selected = [resource for _, resource in parts]
            self.store.set_state(f"mogi_resources_{year}_full", selected)
            return selected

        candidates = [
            r for r in resources
            if str(r.get("format", "")).upper() == "CSV"
            and str(year) in (r.get("name", "") + " " + r.get("description", ""))
            and "IPTU" in (r.get("name", "") + " " + r.get("description", "")).upper()
        ]
        if not candidates:
            raise LookupError(f"Nenhum CSV de IPTU encontrado para o ano {year}.")
        candidates.sort(key=lambda r: ("part" in r.get("name", "").lower(), int(r.get("size") or 0)))
        resource = candidates[0]
        self.store.set_state(f"mogi_resource_{year}", resource)
        return [resource]

    @staticmethod
    def clean_row(row: Dict[str, str], year: int) -> Dict[str, Any]:
        """Conserva o cadastro individual e o identificador territorial sem confundi-los.

        `cadastro_id` identifica a linha cadastral individual. `id_local` é a
        referência territorial compartilhada que aparece repetida em condomínios
        e outros conjuntos de unidades.
        """
        get = lambda *keys: next((row.get(k) for k in keys if row.get(k) not in (None, "")), None)
        cadastro_id = safe_text(get("cadastro_id"))
        id_local = safe_text(get("id_local", "local"))
        parsed = parse_inscricao_mogi(id_local)
        construcoes = []
        for index in range(1, 11):
            area = safe_float(get(f"area{index}"))
            tipo_construcao = safe_text(get(f"construcao{index}"))
            padrao = safe_text(get(f"padrao{index}"))
            descricao_padrao = safe_text(get(f"descr_pd{index}"))
            if not any((area, tipo_construcao, padrao, descricao_padrao)):
                continue
            construcoes.append(
                {
                    "numero": index,
                    "tipo": tipo_construcao,
                    "area_m2": area,
                    "padrao": padrao,
                    "descricao_padrao": descricao_padrao,
                }
            )
        area_construida = round(sum(item.get("area_m2") or 0.0 for item in construcoes), 2)
        props: Dict[str, Any] = {
            "cidade": "Mogi das Cruzes",
            "fonte": "Portal de Dados Abertos - Cadastro Imobiliário",
            "exercicio": safe_text(get("exercicio")) or str(year),
            "cadastro_id": cadastro_id,
            "cadastro_id_normalizado": normalize_inscricao(cadastro_id),
            "id_local": id_local,
            "inscricao_imobiliaria": id_local,
            "situacao_cadastro": safe_text(get(f"situacao({year})", "situacao")),
            "tipo": safe_text(get("classe_fiscal", "tipo")),
            "zona_fiscal": safe_text(get("zona_fiscal")),
            "uso_imovel": safe_text(get("uso_imovel")),
            "numero_imovel_quadra": safe_text(get("nro_imov_qda", "nro_local")),
            "complemento": safe_text(get("complemento")),
            "logradouro": safe_text(get("logradouro")),
            "bairro": safe_text(get("bairro")),
            "codigo_loteamento": safe_text(get("cod_loteamento")),
            "loteamento": safe_text(get("loteamento", "nome_loteamento")),
            "distrito_cadastro": safe_text(get("distrito")),
            "categoria_propriedade": safe_text(get("cat_propriedade")),
            "destinacao_terreno": safe_text(get("destinacao_terr")),
            "situacao_terreno": safe_text(get("situacao_terr")),
            "uso_terreno": safe_text(get("uso_terreno")),
            "topografia_terreno": safe_text(get("topografia_terr")),
            "pedologia_terreno": safe_text(get("pedologia_terr")),
            "ocupacao_imovel": safe_text(get("ocupacao_imovel")),
            "estagio_construcao": safe_text(get("estagio_constr")),
            "situacao_conservacao": safe_text(get("sit_conservacao")),
            "posicao_confrontante": safe_text(get("posicao_confrontante")),
            "situacao_no_terreno": safe_text(get("situacao_no_terr")),
            "tipo_construcao": safe_text(get("tipo_construcao")),
            "cadastro_anterior_id": safe_text(get("cad_anterior_id")),
            "quantidade_cadastros_anteriores": safe_text(get("qtde_cadastro_ant")),
            "ano_construcao": safe_text(get("ano_construcao")),
            "area_terreno_m2": safe_float(get("area_terreno")),
            "testada_m": safe_float(get("testada")),
            "area_construcao_m2": area_construida,
            "construcoes": construcoes,
            "valor_venal": safe_float(get("valor_venal")),
            "moeda": safe_text(get("moeda")),
            "quadra_chave": parsed["quadra_chave"] if parsed else quadra_key_from_inscricao(id_local),
            "precisao_geometria": "sem_geometria",
        }
        # A decomposição abaixo descreve o id_local, não substitui o cadastro_id individual.
        if parsed:
            props.update({
                "setor_fiscal": parsed["setor"],
                "quadra_num": parsed["quadra_num"],
                "lote_num": parsed["lote_num"],
                "id_local_sufixo": parsed["sufixo_local"],
                "lote_chave": parsed["lote_chave"],
                "id_local_formatado": parsed["id_local_formatado"],
            })
        return props


    @staticmethod
    def record_id(props: Dict[str, Any], ordinal: int) -> str:
        """Cria uma chave determinística para permitir atualização idempotente do cadastro."""
        cadastro_id = props.get("cadastro_id_normalizado")
        if cadastro_id:
            return f"cadastro:{cadastro_id}"
        digest = hashlib.sha1(json.dumps(props, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]
        return f"linha:{ordinal}:{digest}"

    def run(
        self,
        year: int,
        force: bool = False,
        full: bool = False,
        limit_resources: Optional[int] = None,
    ) -> Dict[str, int]:
        """Processa os CSVs em streaming e grava lotes pequenos para conter o uso de memória."""
        resources = self.find_resources(year, full=full)
        if force:
            if limit_resources is not None:
                raise ValueError("Não use --force junto com --limit-mogi-resources: a reimportação cadastral precisa ser completa.")
            removed = self.store.clear_source_records(self.source)
            logging.info("Mogi: %s registros da versão anterior removidos antes da reimportação.", removed)
        processed = 0
        inserted = 0
        completed = 0

        for resource in resources:
            if limit_resources is not None and completed >= limit_resources:
                break
            resource_id = resource["id"]
            if self.store.resource_done(self.source, resource_id) and not force:
                logging.info("Mogi: recurso %s já está processado; use --force para reprocessar.", resource_id)
                completed += 1
                continue

            logging.info("Mogi: baixando %s", resource["url"])
            response = self.client.request("GET", resource["url"], stream=True, headers={"Accept": "text/csv,*/*"})
            response.raise_for_status()
            lines = (line.decode("latin-1") for line in response.iter_lines(decode_unicode=False))
            reader = csv.DictReader(lines, delimiter=";")
            batch: List[Dict[str, Any]] = []

            for ordinal, raw in enumerate(reader, start=1):
                props = self.clean_row(raw, year)
                if not props.get("cadastro_id"):
                    continue
                batch.append({"record_id": self.record_id(props, ordinal), "properties": props, "geometry": None})
                processed += 1
                if len(batch) >= 1000:
                    inserted += self.store.upsert_records(self.source, batch)
                    batch.clear()
                    logging.info("Mogi: %s registros processados", processed)

            inserted += self.store.upsert_records(self.source, batch)
            self.store.mark_resource(self.source, resource_id, "done", response.status_code)
            completed += 1

        self.store.set_state(
            "mogi_last_run",
            {"year": year, "full": full, "processed": processed, "resources": completed, "total_resources": len(resources)},
        )
        return {"linhas_processadas": processed, "registros_gravados": inserted, "recursos_processados": completed}


class MogiIptuVenalExtractor:
    """Coleta o recorte de valor venal da base pública de IPTU de Mogi."""

    source = "mogi_iptu_valor_venal_publico"

    def __init__(self, client: PersistentHttpClient, store: CheckpointStore) -> None:
        """Reaproveita a sessão HTTP e o mesmo checkpoint da coleta cadastral."""
        self.client = client
        self.store = store

    def find_resources(self, year: int) -> List[Dict[str, Any]]:
        """Localiza todas as partes pmmc_iptu do exercício na API oficial CKAN."""
        response = self.client.get_json(MOGI_PACKAGE_API)
        if not response.get("success"):
            raise RuntimeError("A API CKAN retornou success=false ao procurar a base de IPTU.")
        pattern = re.compile(rf"^pmmc_iptu_{year}_part(\d+)\.csv$", re.IGNORECASE)
        parts = []
        for resource in response["result"]["resources"]:
            match = pattern.match(str(resource.get("name", "")))
            if match and str(resource.get("format", "")).upper() == "CSV":
                parts.append((int(match.group(1)), resource))
        if not parts:
            raise LookupError(f"Nenhuma parte pmmc_iptu_{year}_partN.csv encontrada na API CKAN.")
        parts.sort(key=lambda item: item[0])
        resources = [resource for _, resource in parts]
        self.store.set_state(f"mogi_iptu_resources_{year}", resources)
        return resources

    @staticmethod
    def clean_row(row: Dict[str, str], year: int, resource_name: str) -> Optional[Dict[str, Any]]:
        """Conserva cadastro, id_local e valores venais, descartando lançamentos do tributo."""
        cadastro_id = safe_text(row.get("cadastro_id"))
        cadastro_id_normalizado = normalize_inscricao(cadastro_id)
        if not cadastro_id_normalizado:
            return None
        terreno = safe_float(row.get("vl_venal_terreno"))
        construcao = safe_float(row.get("vl_venal_construcao"))
        if terreno is None and construcao is None:
            return None
        terreno = terreno or 0.0
        construcao = construcao or 0.0
        exercicio_texto = safe_text(row.get("exercicio"))
        try:
            exercicio = int(exercicio_texto) if exercicio_texto else year
        except ValueError:
            exercicio = year
        return {
            "cadastro_id_normalizado": cadastro_id_normalizado,
            "cadastro_id": cadastro_id,
            "id_local": safe_text(row.get("id_local")),
            "exercicio": exercicio,
            "valor_venal_terreno": terreno,
            "valor_venal_construcao": construcao,
            "valor_venal_total": round(terreno + construcao, 2),
            "tipo_fonte": "iptu",
            "fonte_recurso": resource_name,
        }

    def run(
        self,
        year: int,
        force: bool = False,
        limit_resources: Optional[int] = None,
    ) -> Dict[str, int]:
        """Processa as partes de IPTU em streaming e permite retomada pelo checkpoint."""
        resources = self.find_resources(year)
        processed = 0
        stored = 0
        completed = 0
        for resource in resources:
            if limit_resources is not None and completed >= limit_resources:
                break
            resource_id = resource["id"]
            if self.store.resource_done(self.source, resource_id) and not force:
                logging.info("Mogi/IPTU: recurso %s já processado; continuando.", resource["name"])
                completed += 1
                continue

            logging.info("Mogi/IPTU: baixando %s", resource["name"])
            response = self.client.request("GET", resource["url"], stream=True, headers={"Accept": "text/csv,*/*"})
            response.raise_for_status()
            lines = (line.decode("latin-1") for line in response.iter_lines(decode_unicode=False))
            reader = csv.DictReader(lines, delimiter="|")
            batch: List[Dict[str, Any]] = []
            for raw in reader:
                item = self.clean_row(raw, year, str(resource["name"]))
                if item is None:
                    continue
                batch.append(item)
                processed += 1
                if len(batch) >= 2000:
                    stored += self.store.upsert_mogi_iptu_venal(batch)
                    batch.clear()
                    if processed % 10000 == 0:
                        logging.info("Mogi/IPTU: %s valores venais processados", processed)
            stored += self.store.upsert_mogi_iptu_venal(batch)
            self.store.mark_resource(self.source, resource_id, "done", response.status_code)
            completed += 1

        result = {
            "valores_processados": processed,
            "valores_gravados": stored,
            "recursos_processados": completed,
            "total_recursos": len(resources),
        }
        self.store.set_state(f"mogi_iptu_venal_{year}_last_run", {"year": year, **result})
        return result


class MogiItbiVenalExtractor:
    """Coleta valores venais publicados nos arquivos anuais de IPTU e ITBI."""

    source = "mogi_itbi_valor_venal_publico"

    def __init__(self, client: PersistentHttpClient, store: CheckpointStore) -> None:
        """Usa a sessão e o checkpoint compartilhados pelos extratores de Mogi."""
        self.client = client
        self.store = store

    def find_resource(self, year: int) -> Dict[str, Any]:
        """Localiza o arquivo iptu_itbi do exercício no catálogo oficial."""
        response = self.client.get_json(MOGI_PACKAGE_API)
        expected = f"iptu_itbi_{year}.csv"
        for resource in response.get("result", {}).get("resources", []):
            if str(resource.get("name", "")).casefold() == expected.casefold():
                return resource
        raise LookupError(f"Recurso {expected} não encontrado na API CKAN.")

    @staticmethod
    def clean_row(row: Dict[str, str], year: int, resource_name: str) -> Optional[Dict[str, Any]]:
        """Mantém cadastro, id_local, exercício e valor venal total da linha publicada."""
        cadastro_id = safe_text(row.get("cadastro_id"))
        cadastro_id_normalizado = normalize_inscricao(cadastro_id)
        valor = safe_float(row.get("valor_venal"))
        if not cadastro_id_normalizado or valor is None or valor <= 0:
            return None
        exercicio_texto = safe_text(row.get("exercicio"))
        try:
            exercicio = int(exercicio_texto) if exercicio_texto else year
        except ValueError:
            exercicio = year
        return {
            "cadastro_id_normalizado": cadastro_id_normalizado,
            "cadastro_id": cadastro_id,
            "id_local": safe_text(row.get("local")),
            "exercicio": exercicio,
            "valor_venal_terreno": 0.0,
            "valor_venal_construcao": 0.0,
            "valor_venal_total": valor,
            "tipo_fonte": "itbi",
            "fonte_recurso": resource_name,
        }

    def run(self, year: int, force: bool = False) -> Dict[str, int]:
        """Processa o CSV anual sem substituir um valor de IPTU do mesmo exercício."""
        resource = self.find_resource(year)
        resource_id = resource["id"]
        if self.store.resource_done(self.source, resource_id) and not force:
            logging.info("Mogi/ITBI: recurso %s já processado; continuando.", resource["name"])
            return {"linhas_processadas": 0, "valores_gravados": 0, "recursos_processados": 1}

        logging.info("Mogi/ITBI: baixando %s", resource["name"])
        response = self.client.request(
            "GET", resource["url"], stream=True, headers={"Accept": "text/csv,*/*"}
        )
        response.raise_for_status()
        lines = (line.decode("latin-1") for line in response.iter_lines(decode_unicode=False))
        reader = csv.DictReader(lines, delimiter=";")
        processed = 0
        stored = 0
        batch: List[Dict[str, Any]] = []
        for raw in reader:
            item = self.clean_row(raw, year, str(resource["name"]))
            if item is None:
                continue
            batch.append(item)
            processed += 1
            if len(batch) >= 2000:
                stored += self.store.upsert_mogi_iptu_venal(batch, preserve_existing=True)
                batch.clear()
        stored += self.store.upsert_mogi_iptu_venal(batch, preserve_existing=True)
        self.store.mark_resource(self.source, resource_id, "done", response.status_code)
        result = {"linhas_processadas": processed, "valores_gravados": stored, "recursos_processados": 1}
        self.store.set_state(f"mogi_itbi_venal_{year}_last_run", {"year": year, **result})
        return result


def apply_mogi_iptu_venal(store: CheckpointStore) -> Dict[str, int]:
    """Vincula o valor venal mais recente ao cadastro_id individual correspondente."""
    latest: Dict[str, sqlite3.Row] = {}
    for row in store.iter_mogi_iptu_venal():
        current = latest.get(row["cadastro_id_normalizado"])
        if current is None or row["exercicio"] >= current["exercicio"]:
            latest[row["cadastro_id_normalizado"]] = row

    linked = 0
    linked_historical = 0
    linked_itbi = 0
    batch: List[Dict[str, Any]] = []
    for record in store.iter_records(MogiPublicExtractor.source):
        props = json.loads(record["properties_json"])
        value = latest.get(normalize_inscricao(props.get("cadastro_id")))
        if value is None:
            props["possui_valor_venal_publico"] = False
            tipo_normalizado = normalized_field_name(props.get("tipo"))
            situacao_normalizada = normalized_field_name(props.get("situacao_cadastro"))
            if "desativado" in tipo_normalizado or situacao_normalizada == "inativo":
                props["situacao_valor_venal"] = "indisponivel_cadastro_desativado"
            elif "imune" in tipo_normalizado:
                props["situacao_valor_venal"] = "indisponivel_cadastro_imune"
            elif "isento" in tipo_normalizado:
                props["situacao_valor_venal"] = "indisponivel_cadastro_isento"
            else:
                props["situacao_valor_venal"] = "indisponivel_nas_bases_publicas_2016_2024"
            props["valor_venal_requer_consulta_individual"] = True
            batch.append(
                {
                    "record_id": record["record_id"],
                    "properties": props,
                    "geometry": json.loads(record["geometry_json"]) if record["geometry_json"] else None,
                }
            )
            if len(batch) >= 1000:
                store.upsert_records(MogiPublicExtractor.source, batch)
                batch.clear()
            continue
        tipo_fonte = value["tipo_fonte"]
        if tipo_fonte == "iptu":
            props["valor_venal_terreno"] = value["valor_venal_terreno"]
            props["valor_venal_construcao"] = value["valor_venal_construcao"]
        else:
            props.pop("valor_venal_terreno", None)
            props.pop("valor_venal_construcao", None)
        props["valor_venal"] = value["valor_venal_total"]
        props["exercicio_valor_venal"] = value["exercicio"]
        props["moeda"] = "BRL"
        props["tipo_fonte_valor_venal"] = tipo_fonte
        props["recurso_fonte_valor_venal"] = value["fonte_recurso"]
        props["fonte_valor_venal"] = (
            "Portal de Dados Abertos - Base pública de IPTU"
            if tipo_fonte == "iptu"
            else "Portal de Dados Abertos - Base pública de IPTU e ITBI"
        )
        try:
            cadastro_year = int(props.get("exercicio") or value["exercicio"])
        except (TypeError, ValueError):
            cadastro_year = value["exercicio"]
        props["valor_venal_historico"] = value["exercicio"] < cadastro_year
        props["possui_valor_venal_publico"] = True
        props["situacao_valor_venal"] = (
            "disponivel_historico" if props["valor_venal_historico"] else "disponivel_exercicio_principal"
        )
        props["valor_venal_requer_consulta_individual"] = False
        if props["valor_venal_historico"]:
            linked_historical += 1
        if tipo_fonte == "itbi":
            linked_itbi += 1
        batch.append(
            {
                "record_id": record["record_id"],
                "properties": props,
                "geometry": json.loads(record["geometry_json"]) if record["geometry_json"] else None,
            }
        )
        linked += 1
        if len(batch) >= 1000:
            store.upsert_records(MogiPublicExtractor.source, batch)
            batch.clear()
    if batch:
        store.upsert_records(MogiPublicExtractor.source, batch)
    return {
        "valores_disponiveis": len(latest),
        "cadastros_vinculados": linked,
        "cadastros_com_valor_historico": linked_historical,
        "cadastros_com_valor_itbi": linked_itbi,
    }


def georeference_mogi_records(store: CheckpointStore) -> Dict[str, int]:
    """Associa cada cadastro de Mogi à quadra pública ou, em último caso, ao logradouro."""
    quadras: Dict[str, List[Dict[str, Any]]] = {}
    for row in store.iter_records(MogiQuadrasExtractor.source):
        props = json.loads(row["properties_json"])
        geometry = json.loads(row["geometry_json"])
        key = normalize_quadra_key(props.get("quadra_chave"))
        if key:
            quadras.setdefault(key, []).append(geometry)

    logradouros: Dict[str, List[Dict[str, Any]]] = {}
    nomes_logradouros: Dict[str, str] = {}
    for row in store.iter_records(MogiLogradourosExtractor.source):
        props = json.loads(row["properties_json"])
        geometry = json.loads(row["geometry_json"])
        key = logradouro_key(props.get("logradouro_chave") or props.get("logradouro"))
        if key:
            logradouros.setdefault(key, []).append(geometry)
            nomes_logradouros.setdefault(key, safe_text(props.get("logradouro")) or key)

    logradouro_keys = sorted(logradouros)

    def approximate_logradouro(raw_name: Any) -> Optional[Tuple[str, float]]:
        """Aceita somente uma correspondência textual forte e sem segundo candidato próximo."""
        key = logradouro_key(raw_name)
        if not key or len(key) < 8 or "ENCRAVADO" in key or "ERRO" in key:
            return None
        matches = difflib.get_close_matches(key, logradouro_keys, n=2, cutoff=0.92)
        if not matches:
            return None
        best_score = difflib.SequenceMatcher(None, key, matches[0]).ratio()
        second_score = difflib.SequenceMatcher(None, key, matches[1]).ratio() if len(matches) > 1 else 0.0
        if best_score < 0.92 or best_score - second_score < 0.05:
            return None
        return matches[0], best_score

    batch: List[Dict[str, Any]] = []
    matched = 0
    fallback_logradouro = 0
    fallback_logradouro_aproximado = 0
    unmatched = 0
    invalid = 0
    for row in store.iter_records(MogiPublicExtractor.source):
        props = json.loads(row["properties_json"])
        geometries = quadras.get(normalize_quadra_key(props.get("quadra_chave")) or "", [])
        geometry: Optional[Dict[str, Any]] = None
        boxes = [geometry_bbox(candidate) for candidate in geometries]
        boxes = [box for box in boxes if box is not None]
        if boxes:
            minx = min(box[0] for box in boxes)
            miny = min(box[1] for box in boxes)
            maxx = max(box[2] for box in boxes)
            maxy = max(box[3] for box in boxes)
            geometry = {"type": "Point", "coordinates": [round((minx + maxx) / 2, 7), round((miny + maxy) / 2, 7)]}
            props["precisao_geometria"] = "centro_da_quadra"
            props["metodo_georreferenciamento"] = "chave_cadastral_para_quadra_publica"
            props["quadras_geometricas_associadas"] = len(boxes)
            matched += 1
        else:
            logradouro_normalizado = logradouro_key(props.get("logradouro")) or ""
            geometries = logradouros.get(logradouro_normalizado, [])
            approximate_match: Optional[Tuple[str, float]] = None
            if not geometries:
                approximate_match = approximate_logradouro(props.get("logradouro"))
                if approximate_match:
                    geometries = logradouros.get(approximate_match[0], [])
            boxes = [geometry_bbox(candidate) for candidate in geometries]
            boxes = [box for box in boxes if box is not None]
        if geometry is None and boxes:
            minx = min(box[0] for box in boxes)
            miny = min(box[1] for box in boxes)
            maxx = max(box[2] for box in boxes)
            maxy = max(box[3] for box in boxes)
            geometry = {"type": "Point", "coordinates": [round((minx + maxx) / 2, 7), round((miny + maxy) / 2, 7)]}
            if approximate_match:
                props["precisao_geometria"] = "centro_do_logradouro_aproximado"
                props["metodo_georreferenciamento"] = "logradouro_publico_correspondencia_textual_forte"
                props["logradouro_publico_correspondente"] = nomes_logradouros.get(approximate_match[0])
                props["score_correspondencia_logradouro"] = round(approximate_match[1], 4)
                fallback_logradouro_aproximado += 1
            else:
                props["precisao_geometria"] = "centro_do_logradouro"
                props["metodo_georreferenciamento"] = "logradouro_publico_normalizado"
                props.pop("logradouro_publico_correspondente", None)
                props.pop("score_correspondencia_logradouro", None)
                fallback_logradouro += 1
            props["logradouros_geometricos_associados"] = len(boxes)
        elif geometry is None:
            props["precisao_geometria"] = "sem_geometria"
            props["metodo_georreferenciamento"] = "quadra_e_logradouro_publicos_nao_encontrados"
            if geometries:
                invalid += 1
            else:
                unmatched += 1

        batch.append({"record_id": row["record_id"], "properties": props, "geometry": geometry})
        if len(batch) >= 1000:
            store.upsert_records(MogiPublicExtractor.source, batch)
            batch.clear()
    if batch:
        store.upsert_records(MogiPublicExtractor.source, batch)

    result = {
        "georreferenciados_por_quadra": matched,
        "georreferenciados_por_logradouro": fallback_logradouro,
        "georreferenciados_por_logradouro_aproximado": fallback_logradouro_aproximado,
        "sem_geometria": unmatched,
        "geometrias_invalidas": invalid,
    }
    store.set_state("mogi_georreferenciamento", result)
    return result


# --------------------- Importações locais de documentos --------------------

VENAL_CITY_LABELS = {
    "mogi": "Mogi das Cruzes",
    "itanhaem": "Itanhaém",
}

VENAL_FIELD_ALIASES = {
    "inscricao_imobiliaria": ("inscricao_imobiliaria", "inscricao", "cadastro", "codigo_imovel", "codigo_contribuinte"),
    "exercicio": ("exercicio", "ano", "ano_exercicio"),
    "valor_venal": ("valor_venal", "valor_venal_total", "valor"),
    "numero_certidao": ("numero_certidao", "certidao", "id_certidao", "codigo_certidao"),
    "data_emissao": ("data_emissao", "emissao", "data_certidao"),
    "moeda": ("moeda", "currency"),
}

FISCAL_STATUS_FIELD_ALIASES = {
    "inscricao_imobiliaria": ("inscricao_imobiliaria", "inscricao", "cadastro", "codigo_imovel"),
    "competencia": ("competencia", "exercicio", "ano", "ano_exercicio", "referencia"),
    "possui_pendencia": ("possui_pendencia", "pendencia", "ha_pendencia", "tem_pendencia", "em_aberto"),
    "status_pendencia": ("status_pendencia", "situacao", "status", "situacao_fiscal", "resultado"),
    "valor_total_pendencia": (
        "valor_total_pendencia", "valor_total", "total_pendente", "total_em_aberto",
        "valor_debito", "total_debito", "debito_total", "valor_divida", "total_divida",
    ),
    "quantidade_pendencias": ("quantidade_pendencias", "qtd_pendencias", "quantidade", "qtd"),
    "fonte_consulta": ("fonte_consulta", "fonte", "origem", "orgao"),
    "data_consulta": ("data_consulta", "consulta_em", "data", "emissao", "data_emissao"),
    "numero_documento": ("numero_documento", "numero_certidao", "certidao", "protocolo", "processo"),
}

PII_OR_FINANCIAL_FIELD_MARKERS = (
    "cpf", "cnpj", "propriet", "titular", "contribuinte", "nome", "rg", "telefone",
    "celular", "email", "e_mail", "boleto", "codigo_barras", "pagamento", "vencimento",
    "parcela", "divida", "debito", "pix",
)

FISCAL_UNSAFE_FIELD_MARKERS = (
    "cpf", "cnpj", "propriet", "titular", "contribuinte", "nome", "rg", "telefone",
    "celular", "email", "e_mail", "boleto", "codigo_barras", "linha_digitavel",
    "pagamento", "vencimento", "parcela", "pix", "nosso_numero", "agencia", "conta",
)


def normalized_field_name(value: Any) -> str:
    """Normaliza cabeçalhos de arquivos para aceitar variações de caixa, acento e separador."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")


def normalize_inscricao(value: Any) -> str:
    """Normaliza uma inscrição sem adivinhar dígitos nem alterar zeros à esquerda."""
    return re.sub(r"[^0-9a-zA-Z]", "", str(value or "")).upper()


def parse_brl_number(value: Any) -> Optional[float]:
    """Lê um número positivo em formatos brasileiros ou decimais sem perder a parte fracionária."""
    if value is None:
        return None
    text = str(value).strip().replace("R$", "").replace(" ", "")
    if not text:
        return None
    text = re.sub(r"[^0-9,.-]", "", text)
    if not text:
        return None
    last_comma = text.rfind(",")
    last_dot = text.rfind(".")
    if last_comma >= 0 and last_dot >= 0:
        decimal_mark = "," if last_comma > last_dot else "."
        thousand_mark = "." if decimal_mark == "," else ","
        text = text.replace(thousand_mark, "").replace(decimal_mark, ".")
    elif last_comma >= 0:
        text = text.replace(",", ".")
    elif text.count(".") > 1 or (last_dot >= 0 and len(text) - last_dot - 1 == 3):
        text = text.replace(".", "")
    try:
        parsed = float(text)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _field_value(row: Dict[str, Any], field: str) -> Optional[Any]:
    """Encontra o primeiro campo preenchido entre os nomes aceitos para uma coluna venal."""
    normalized = {normalized_field_name(key): value for key, value in row.items()}
    for alias in VENAL_FIELD_ALIASES[field]:
        value = normalized.get(alias)
        if value is not None and str(value).strip():
            return value
    return None


def _field_value_from_aliases(row: Dict[str, Any], aliases: Dict[str, Tuple[str, ...]], field: str) -> Optional[Any]:
    """Encontra uma coluna usando o mapa de aliases informado."""
    normalized = {normalized_field_name(key): value for key, value in row.items()}
    for alias in aliases[field]:
        value = normalized.get(alias)
        if value is not None and str(value).strip():
            return value
    return None


def _validate_venal_headers(headers: Iterable[Any]) -> None:
    """Interrompe a importação quando o arquivo possui campos pessoais ou financeiros indevidos."""
    unsafe = []
    for header in headers:
        name = normalized_field_name(header)
        if any(marker in name for marker in PII_OR_FINANCIAL_FIELD_MARKERS):
            unsafe.append(str(header))
    if unsafe:
        fields = ", ".join(sorted(set(unsafe)))
        raise ValueError(
            "O arquivo de certidões contém campos pessoais, de boleto ou pagamento "
            f"não necessários e foi recusado: {fields}. Exporte somente inscrição, exercício, "
            "valor venal, número/data da certidão e moeda."
        )


def _validate_fiscal_headers(headers: Iterable[Any]) -> None:
    """Recusa campos que indicam boleto, pessoa física/jurídica ou pagamento individual."""
    unsafe = []
    for header in headers:
        name = normalized_field_name(header)
        if any(marker in name for marker in FISCAL_UNSAFE_FIELD_MARKERS):
            unsafe.append(str(header))
    if unsafe:
        fields = ", ".join(sorted(set(unsafe)))
        raise ValueError(
            "O arquivo de situação fiscal contém campos pessoais ou de boleto "
            f"e foi recusado: {fields}. Exporte somente inscrição, status de pendência, "
            "competência, fonte, data e total agregado quando necessário."
        )


def read_venal_rows(path: Path) -> List[Tuple[int, Dict[str, Any]]]:
    """Lê planilha local já autorizada; nenhuma chamada é feita ao portal municipal."""
    if not path.is_file():
        raise ValueError(f"Arquivo de certidões não encontrado: {path}")

    if path.suffix.casefold() == ".json":
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict):
            for key in ("registros", "records", "certidoes", "certidoes_venais", "data"):
                if isinstance(raw.get(key), list):
                    raw = raw[key]
                    break
        if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
            raise ValueError("O JSON deve ser uma lista de objetos, ou conter uma lista em 'registros' ou 'certidoes'.")
        headers = [key for item in raw for key in item.keys()]
        _validate_venal_headers(headers)
        return list(enumerate(raw, start=1))

    if path.suffix.casefold() not in (".csv", ".txt"):
        raise ValueError("Use um arquivo CSV, TXT delimitado ou JSON para importar certidões venais.")

    last_error: Optional[Exception] = None
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            with path.open("r", encoding=encoding, newline="") as fh:
                sample = fh.read(4096)
                fh.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
                except csv.Error:
                    dialect = csv.excel
                    dialect.delimiter = ";"
                reader = csv.DictReader(fh, dialect=dialect)
                if not reader.fieldnames:
                    raise ValueError("O CSV não possui cabeçalho.")
                _validate_venal_headers(reader.fieldnames)
                return [(line, dict(row)) for line, row in enumerate(reader, start=2)]
        except UnicodeDecodeError as exc:
            last_error = exc
    raise ValueError(f"Não foi possível ler o arquivo CSV: {last_error}")


def read_fiscal_status_rows(path: Path) -> List[Tuple[int, Dict[str, Any]]]:
    """Lê situação fiscal obtida fora do script, sem baixar boleto ou consultar portal."""
    if not path.is_file():
        raise ValueError(f"Arquivo de situação fiscal não encontrado: {path}")

    if path.suffix.casefold() == ".json":
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict):
            for key in ("registros", "records", "situacoes", "pendencias", "data"):
                if isinstance(raw.get(key), list):
                    raw = raw[key]
                    break
        if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
            raise ValueError("O JSON deve ser uma lista de objetos, ou conter uma lista em 'registros' ou 'pendencias'.")
        headers = [key for item in raw for key in item.keys()]
        _validate_fiscal_headers(headers)
        return list(enumerate(raw, start=1))

    if path.suffix.casefold() not in (".csv", ".txt"):
        raise ValueError("Use um arquivo CSV, TXT delimitado ou JSON para importar situação fiscal.")

    last_error: Optional[Exception] = None
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            with path.open("r", encoding=encoding, newline="") as fh:
                sample = fh.read(4096)
                fh.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
                except csv.Error:
                    dialect = csv.excel
                    dialect.delimiter = ";"
                reader = csv.DictReader(fh, dialect=dialect)
                if not reader.fieldnames:
                    raise ValueError("O CSV não possui cabeçalho.")
                _validate_fiscal_headers(reader.fieldnames)
                return [(line, dict(row)) for line, row in enumerate(reader, start=2)]
        except UnicodeDecodeError as exc:
            last_error = exc
    raise ValueError(f"Não foi possível ler o arquivo CSV: {last_error}")


def parse_pendencia_bool(value: Any, status: Any = None) -> Optional[bool]:
    """Transforma respostas comuns de certidões em verdadeiro/falso."""
    candidates = [value, status]
    positive = {
        "1", "s", "sim", "true", "yes", "y", "possui", "pendente",
        "com_pendencia", "com_pendencias", "em_aberto", "aberto",
        "irregular", "positivo", "positiva", "consta",
    }
    negative = {
        "0", "n", "nao", "não", "false", "no", "regular", "quitado",
        "quitada", "sem_pendencia", "sem_pendencias", "nada_consta",
        "negativo", "negativa", "nao_consta", "não_consta",
    }
    for candidate in candidates:
        text = normalized_field_name(candidate)
        if not text:
            continue
        if text in negative or any(marker in text for marker in ("sem_pendencia", "nada_consta", "quitad")):
            return False
        if any(marker in text for marker in ("sem_debito", "sem_debitos", "sem_divida", "sem_dividas")):
            return False
        if text in positive or any(marker in text for marker in ("em_aberto", "pendente", "irregular")):
            return True
        if any(marker in text for marker in ("debito", "debitos", "divida", "dividas")):
            return True
    return None


def import_fiscal_status(store: CheckpointStore, path: Path, city_key: str) -> Dict[str, int]:
    """Importa status de pendência por inscrição para cruzar com o cadastro público."""
    city = VENAL_CITY_LABELS[city_key]
    prepared: List[Dict[str, Any]] = []
    invalid = 0
    for line, row in read_fiscal_status_rows(path):
        inscricao = str(_field_value_from_aliases(row, FISCAL_STATUS_FIELD_ALIASES, "inscricao_imobiliaria") or "").strip()
        competencia = str(_field_value_from_aliases(row, FISCAL_STATUS_FIELD_ALIASES, "competencia") or "geral").strip()
        raw_status = _field_value_from_aliases(row, FISCAL_STATUS_FIELD_ALIASES, "status_pendencia")
        raw_bool = _field_value_from_aliases(row, FISCAL_STATUS_FIELD_ALIASES, "possui_pendencia")
        possui_pendencia = parse_pendencia_bool(raw_bool, raw_status)
        status_pendencia = str(raw_status or ("com_pendencia" if possui_pendencia else "sem_pendencia")).strip()
        valor_total = parse_brl_number(_field_value_from_aliases(row, FISCAL_STATUS_FIELD_ALIASES, "valor_total_pendencia"))
        qtd_raw = _field_value_from_aliases(row, FISCAL_STATUS_FIELD_ALIASES, "quantidade_pendencias")
        try:
            quantidade = int(str(qtd_raw).strip()) if qtd_raw is not None and str(qtd_raw).strip() else None
        except ValueError:
            quantidade = None

        if not normalize_inscricao(inscricao) or possui_pendencia is None:
            invalid += 1
            logging.warning("Situação fiscal ignorada na linha %s: inscrição ou status inválido.", line)
            continue

        prepared.append(
            {
                "cidade": city,
                "inscricao_normalizada": normalize_inscricao(inscricao),
                "inscricao_imobiliaria": inscricao,
                "competencia": competencia or "geral",
                "possui_pendencia": possui_pendencia,
                "status_pendencia": status_pendencia,
                "valor_total_pendencia": valor_total,
                "quantidade_pendencias": quantidade,
                "fonte_consulta": str(_field_value_from_aliases(row, FISCAL_STATUS_FIELD_ALIASES, "fonte_consulta") or "").strip() or None,
                "data_consulta": str(_field_value_from_aliases(row, FISCAL_STATUS_FIELD_ALIASES, "data_consulta") or "").strip() or None,
                "numero_documento": str(_field_value_from_aliases(row, FISCAL_STATUS_FIELD_ALIASES, "numero_documento") or "").strip() or None,
                "arquivo_origem": path.name,
                "linha_origem": line,
            }
        )

    imported = store.upsert_fiscal_status(prepared)
    result = {"linhas_lidas": len(prepared) + invalid, "importadas": imported, "invalidas": invalid}
    store.set_state("fiscal_status_last_import", {"cidade": city, "arquivo": path.name, **result})
    return result


def import_venal_validations(store: CheckpointStore, path: Path, city_key: str) -> Dict[str, int]:
    """Valida e importa uma lista local de valores venais para o município informado."""
    city = VENAL_CITY_LABELS[city_key]
    prepared: List[Dict[str, Any]] = []
    invalid = 0
    for line, row in read_venal_rows(path):
        inscricao = str(_field_value(row, "inscricao_imobiliaria") or "").strip()
        exercicio_raw = _field_value(row, "exercicio")
        valor = parse_brl_number(_field_value(row, "valor_venal"))
        try:
            exercicio = int(str(exercicio_raw).strip())
        except (TypeError, ValueError):
            exercicio = 0
        if not normalize_inscricao(inscricao) or not (1900 <= exercicio <= 2100) or valor is None:
            invalid += 1
            logging.warning("Certidão venal ignorada na linha %s: inscrição, exercício ou valor inválido.", line)
            continue
        moeda = str(_field_value(row, "moeda") or "BRL").strip().upper()
        if moeda not in ("BRL", "R$"):
            logging.warning("Certidão venal ignorada na linha %s: moeda não suportada.", line)
            invalid += 1
            continue
        prepared.append(
            {
                "cidade": city,
                "inscricao_normalizada": normalize_inscricao(inscricao),
                "inscricao_imobiliaria": inscricao,
                "exercicio": exercicio,
                "valor_venal": valor,
                "moeda": "BRL",
                "numero_certidao": str(_field_value(row, "numero_certidao") or "").strip() or None,
                "data_emissao": str(_field_value(row, "data_emissao") or "").strip() or None,
                "arquivo_origem": path.name,
                "linha_origem": line,
            }
        )

    imported = store.upsert_venal_validations(prepared)
    result = {"linhas_lidas": len(prepared) + invalid, "importadas": imported, "invalidas": invalid}
    store.set_state("venal_last_import", {"cidade": city, "arquivo": path.name, **result})
    return result


def public_cadastre_index(store: CheckpointStore) -> Dict[Tuple[str, str], List[str]]:
    """Indexa as inscrições públicas de Mogi para vincular certidões sem alterar a fonte original."""
    index: Dict[Tuple[str, str], List[str]] = {}
    for row in store.iter_records(MogiPublicExtractor.source):
        props = json.loads(row["properties_json"])
        city = str(props.get("cidade") or "").strip()
        inscricao = normalize_inscricao(props.get("inscricao_imobiliaria"))
        if city and inscricao:
            index.setdefault((city, inscricao), []).append(row["record_id"])
    return index


def apply_venal_validations(store: CheckpointStore) -> Dict[str, int]:
    """Anexa a certidão importada ao cadastro público correspondente, sem substituir a fonte original."""
    validations: Dict[Tuple[str, str], sqlite3.Row] = {}
    for row in store.iter_venal_validations():
        key = (row["cidade"], row["inscricao_normalizada"])
        previous = validations.get(key)
        if previous is None or row["exercicio"] > previous["exercicio"]:
            validations[key] = row
    if not validations:
        return {"validacoes": 0, "cadastros_vinculados": 0, "sem_cadastro_publico": 0}

    index = public_cadastre_index(store)
    batch: List[Dict[str, Any]] = []
    linked_ids = set()
    for row in store.iter_records(MogiPublicExtractor.source):
        props = json.loads(row["properties_json"])
        validation = validations.get((str(props.get("cidade") or "").strip(), normalize_inscricao(props.get("inscricao_imobiliaria"))))
        if validation is None:
            continue
        props["valor_venal_certidao"] = validation["valor_venal"]
        props["exercicio_certidao_venal"] = validation["exercicio"]
        props["moeda_certidao_venal"] = validation["moeda"]
        props["numero_certidao_venal"] = validation["numero_certidao"]
        props["data_emissao_certidao_venal"] = validation["data_emissao"]
        props["fonte_valor_venal_certidao"] = "certidao_importada_localmente"
        batch.append({"record_id": row["record_id"], "properties": props, "geometry": json.loads(row["geometry_json"]) if row["geometry_json"] else None})
        linked_ids.add((validation["cidade"], validation["inscricao_normalizada"]))
        if len(batch) >= 1000:
            store.upsert_records(MogiPublicExtractor.source, batch)
            batch.clear()
    if batch:
        store.upsert_records(MogiPublicExtractor.source, batch)

    unmatched = len([key for key in validations if key not in index])
    result = {"validacoes": len(validations), "cadastros_vinculados": len(linked_ids), "sem_cadastro_publico": unmatched}
    store.set_state("venal_vinculacao", result)
    return result


def apply_fiscal_status(store: CheckpointStore) -> Dict[str, int]:
    """Anexa o status fiscal importado ao cadastro público correspondente."""
    statuses: Dict[Tuple[str, str], sqlite3.Row] = {}
    for row in store.iter_fiscal_status():
        key = (row["cidade"], row["inscricao_normalizada"])
        previous = statuses.get(key)
        previous_marker = "" if previous is None else str(previous["data_consulta"] or previous["competencia"] or "")
        current_marker = str(row["data_consulta"] or row["competencia"] or "")
        if previous is None or current_marker >= previous_marker:
            statuses[key] = row
    if not statuses:
        return {"situacoes": 0, "cadastros_vinculados": 0, "sem_cadastro_publico": 0}

    index = public_cadastre_index(store)
    batch: List[Dict[str, Any]] = []
    linked_ids = set()
    for row in store.iter_records(MogiPublicExtractor.source):
        props = json.loads(row["properties_json"])
        status = statuses.get((str(props.get("cidade") or "").strip(), normalize_inscricao(props.get("inscricao_imobiliaria"))))
        if status is None:
            continue
        props["pendencia_fiscal_possui"] = bool(status["possui_pendencia"])
        props["pendencia_fiscal_status"] = status["status_pendencia"]
        props["pendencia_fiscal_competencia"] = status["competencia"]
        props["pendencia_fiscal_valor_total"] = status["valor_total_pendencia"]
        props["pendencia_fiscal_quantidade"] = status["quantidade_pendencias"]
        props["pendencia_fiscal_fonte"] = status["fonte_consulta"]
        props["pendencia_fiscal_data_consulta"] = status["data_consulta"]
        props["pendencia_fiscal_numero_documento"] = status["numero_documento"]
        props["fonte_pendencia_fiscal"] = "situacao_fiscal_importada_localmente"
        batch.append({"record_id": row["record_id"], "properties": props, "geometry": json.loads(row["geometry_json"]) if row["geometry_json"] else None})
        linked_ids.add((status["cidade"], status["inscricao_normalizada"]))
        if len(batch) >= 1000:
            store.upsert_records(MogiPublicExtractor.source, batch)
            batch.clear()
    if batch:
        store.upsert_records(MogiPublicExtractor.source, batch)

    unmatched = len([key for key in statuses if key not in index])
    result = {"situacoes": len(statuses), "cadastros_vinculados": len(linked_ids), "sem_cadastro_publico": unmatched}
    store.set_state("fiscal_status_vinculacao", result)
    return result


# -------------------------- Exportadores --------------------------

def record_matches_city(row: sqlite3.Row, city: Optional[str]) -> bool:
    """Filtra um registro pela cidade sem depender do nome técnico da fonte."""
    if city is None:
        return True
    props = json.loads(row["properties_json"])
    return normalized_field_name(props.get("cidade")) == normalized_field_name(city)


def export_json(store: CheckpointStore, output: Path, city: Optional[str] = None) -> int:
    """Gera um JSON consolidado a partir do checkpoint, sem depender da execução da coleta."""
    count = 0
    with output.open("w", encoding="utf-8") as fh:
        fh.write("[\n")
        first = True
        for row in store.iter_records():
            if not record_matches_city(row, city):
                continue
            record = json.loads(row["properties_json"])
            record["id"] = row["record_id"]
            record["fonte_id"] = row["source"]
            if row["geometry_json"]:
                record["geometry"] = json.loads(row["geometry_json"])
            if not first:
                fh.write(",\n")
            json.dump(record, fh, ensure_ascii=False, separators=(",", ":"))
            first = False
            count += 1
        fh.write("\n]\n")
    return count


def export_geojson(store: CheckpointStore, output: Path, city: Optional[str] = None) -> int:
    """Gera GeoJSON apenas com registros que possuem referência espacial disponível."""
    count = 0
    with output.open("w", encoding="utf-8") as fh:
        fh.write('{"type":"FeatureCollection","features":[\n')
        first = True
        for row in store.iter_records():
            if not row["geometry_json"] or not record_matches_city(row, city):
                continue
            props = json.loads(row["properties_json"])
            props["id"] = row["record_id"]
            props["fonte_id"] = row["source"]
            feature = {"type": "Feature", "geometry": json.loads(row["geometry_json"]), "properties": props}
            if not first:
                fh.write(",\n")
            json.dump(feature, fh, ensure_ascii=False, separators=(",", ":"))
            first = False
            count += 1
        fh.write("\n]}\n")
    return count


def export_csv(store: CheckpointStore, output: Path, city: Optional[str] = None) -> int:
    """Gera uma visão tabular dos atributos; a geometria permanece no arquivo GeoJSON."""
    fields = [
        "id", "fonte_id", "cidade", "fonte", "camada", "gid", "tipo", "descricao", "bairro",
        "loteamento", "lei_corredor", "restricao", "exercicio", "uso_imovel", "inscricao_imobiliaria",
        "numero_imovel_quadra", "complemento", "logradouro", "area_terreno_m2", "area_construcao_m2",
        "valor_venal", "valor_venal_terreno", "valor_venal_construcao", "exercicio_valor_venal",
        "moeda", "tipo_fonte_valor_venal", "recurso_fonte_valor_venal", "valor_venal_historico",
        "fonte_valor_venal", "quadra_chave", "precisao_geometria", "metodo_georreferenciamento",
        "valor_venal_certidao", "exercicio_certidao_venal", "moeda_certidao_venal",
        "numero_certidao_venal", "data_emissao_certidao_venal", "fonte_valor_venal_certidao",
        "pendencia_fiscal_possui", "pendencia_fiscal_status", "pendencia_fiscal_competencia",
        "pendencia_fiscal_valor_total", "pendencia_fiscal_quantidade", "pendencia_fiscal_fonte",
        "pendencia_fiscal_data_consulta", "pendencia_fiscal_numero_documento", "fonte_pendencia_fiscal",
    ]
    count = 0
    with output.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in store.iter_records():
            if not record_matches_city(row, city):
                continue
            props = json.loads(row["properties_json"])
            props["id"] = row["record_id"]
            props["fonte_id"] = row["source"]
            writer.writerow(props)
            count += 1
    return count


def export_mogi_cadastros_csv(store: CheckpointStore, output: Path) -> int:
    """Exporta somente os cadastros individuais de Mogi, sem misturar camadas do mapa."""
    preferred = [
        "cadastro_id", "cadastro_id_normalizado", "id_local", "exercicio", "situacao_cadastro",
        "tipo", "uso_imovel", "logradouro", "numero_imovel_quadra", "complemento",
        "loteamento", "distrito_cadastro", "zona_fiscal", "area_terreno_m2",
        "area_construcao_m2", "testada_m", "ano_construcao", "valor_venal",
        "valor_venal_terreno", "valor_venal_construcao", "exercicio_valor_venal",
        "tipo_fonte_valor_venal", "situacao_terreno", "uso_terreno", "topografia_terreno",
        "pedologia_terreno", "ocupacao_imovel", "estagio_construcao", "situacao_conservacao",
        "categoria_propriedade", "destinacao_terreno", "posicao_confrontante",
        "situacao_no_terreno", "tipo_construcao", "construcoes", "quadra_chave",
        "precisao_geometria", "metodo_georreferenciamento", "fonte", "fonte_valor_venal",
    ]
    rows = list(store.iter_records(MogiPublicExtractor.source))
    all_fields = {
        name
        for row in rows
        for name in json.loads(row["properties_json"])
    }
    fields = preferred + sorted(all_fields - set(preferred))
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for stored in rows:
            properties = json.loads(stored["properties_json"])
            for name, value in list(properties.items()):
                if isinstance(value, (dict, list)):
                    properties[name] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            writer.writerow(properties)
    return len(rows)


def export_mogi_iptu_venal_csv(
    store: CheckpointStore, output: Path, latest_only: bool = False
) -> int:
    """Exporta o histórico completo ou somente o valor mais recente de cada inscrição."""
    fields = [
        "cadastro_id", "id_local", "exercicio", "valor_venal_terreno",
        "valor_venal_construcao", "valor_venal_total", "tipo_fonte",
        "fonte_recurso", "importado_em",
    ]
    rows = list(store.iter_mogi_iptu_venal())
    if latest_only:
        latest: Dict[str, sqlite3.Row] = {}
        for row in rows:
            current = latest.get(row["cadastro_id_normalizado"])
            if current is None or row["exercicio"] >= current["exercicio"]:
                latest[row["cadastro_id_normalizado"]] = row
        rows = [latest[key] for key in sorted(latest)]
    count = 0
    with output.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "cadastro_id": row["cadastro_id"],
                    "id_local": row["id_local"],
                    "exercicio": row["exercicio"],
                    "valor_venal_terreno": row["valor_venal_terreno"],
                    "valor_venal_construcao": row["valor_venal_construcao"],
                    "valor_venal_total": row["valor_venal_total"],
                    "tipo_fonte": row["tipo_fonte"],
                    "fonte_recurso": row["fonte_recurso"],
                    "importado_em": row["imported_at"],
                }
            )
            count += 1
    return count


def build_mogi_canonical_records(store: CheckpointStore) -> List[Dict[str, Any]]:
    """Cria uma visão territorial por id_local sem apagar os cadastros individuais."""
    groups: Dict[str, Dict[str, Any]] = {}
    geometry_rank = {
        "sem_geometria": 0,
        "centro_do_logradouro_aproximado": 1,
        "centro_do_logradouro": 2,
        "centro_da_quadra": 3,
    }
    for row in store.iter_records(MogiPublicExtractor.source):
        props = json.loads(row["properties_json"])
        key = normalize_inscricao(props.get("id_local") or props.get("inscricao_imobiliaria"))
        if not key:
            continue
        geometry = json.loads(row["geometry_json"]) if row["geometry_json"] else None
        score = sum(value not in (None, "", [], {}) for value in props.values())
        group = groups.get(key)
        if group is None:
            group = {
                "properties": dict(props),
                "geometry": geometry,
                "score": score,
                "linhas": 0,
                "tipos": set(),
                "usos": set(),
                "logradouros": set(),
                "numeros": set(),
                "complementos": set(),
                "com_valor": 0,
                "ativos": 0,
            }
            groups[key] = group
        else:
            current_props = group["properties"]
            if score > group["score"]:
                replacement = dict(props)
                for name, value in current_props.items():
                    if replacement.get(name) in (None, "", [], {}) and value not in (None, "", [], {}):
                        replacement[name] = value
                group["properties"] = replacement
                group["score"] = score
            else:
                for name, value in props.items():
                    if current_props.get(name) in (None, "", [], {}) and value not in (None, "", [], {}):
                        current_props[name] = value

            current_precision = group["properties"].get("precisao_geometria") or "sem_geometria"
            candidate_precision = props.get("precisao_geometria") or "sem_geometria"
            if geometry is not None and geometry_rank.get(candidate_precision, 0) > geometry_rank.get(current_precision, 0):
                group["geometry"] = geometry
                group["properties"]["precisao_geometria"] = candidate_precision
                group["properties"]["metodo_georreferenciamento"] = props.get("metodo_georreferenciamento")

        group["linhas"] += 1
        if props.get("tipo"):
            group["tipos"].add(str(props["tipo"]))
        if props.get("uso_imovel"):
            group["usos"].add(str(props["uso_imovel"]))
        if props.get("logradouro"):
            group["logradouros"].add(str(props["logradouro"]))
        if props.get("numero_imovel_quadra"):
            group["numeros"].add(str(props["numero_imovel_quadra"]))
        if props.get("complemento"):
            group["complementos"].add(str(props["complemento"]))
        if props.get("valor_venal") is not None:
            group["com_valor"] += 1
        if normalized_field_name(props.get("situacao_cadastro")) == "ativo":
            group["ativos"] += 1

    canonical: List[Dict[str, Any]] = []
    for key in sorted(groups):
        group = groups[key]
        representative = group["properties"]
        props = {
            "cidade": "Mogi das Cruzes",
            "fonte": "Portal de Dados Abertos - agrupamento territorial por id_local",
            "id_local": representative.get("id_local") or representative.get("inscricao_imobiliaria"),
            "quadra_chave": representative.get("quadra_chave"),
            "precisao_geometria": representative.get("precisao_geometria"),
            "metodo_georreferenciamento": representative.get("metodo_georreferenciamento"),
            "logradouro_publico_correspondente": representative.get("logradouro_publico_correspondente"),
        }
        props["id_local_normalizado"] = key
        props["quantidade_cadastros_no_local"] = group["linhas"]
        props["quantidade_cadastros_ativos"] = group["ativos"]
        props["quantidade_cadastros_com_valor_venal"] = group["com_valor"]
        props["logradouros_encontrados"] = sorted(group["logradouros"])
        props["numeros_encontrados"] = sorted(group["numeros"])
        props["complementos_encontrados"] = sorted(group["complementos"])
        props["tipos_cadastrais_encontrados"] = sorted(group["tipos"])
        props["usos_imovel_encontrados"] = sorted(group["usos"])
        canonical.append(
            {
                "id": f"mogi-local:{key}",
                "properties": props,
                "geometry": group["geometry"],
            }
        )
    return canonical


def apply_mogi_territorial_context(
    records: List[Dict[str, Any]], store: CheckpointStore
) -> Dict[str, int]:
    """Cruza o ponto de referência de cada inscrição com camadas territoriais oficiais.

    O resultado é contexto espacial do ponto disponível. Ele não transforma a
    quadra ou o centro do logradouro em geometria exata do lote.
    """
    layer_sources = {
        "bairro": "mogi_bairro_geomogi",
        "distrito": "mogi_distrito_geomogi",
        "macrozona": "mogi_macrozona_geomogi",
        "zoneamento": "mogi_zoneamento_geomogi",
    }
    indexes: Dict[str, List[Tuple[Tuple[float, float, float, float], Dict[str, Any], Dict[str, Any]]]] = {}
    for layer, source in layer_sources.items():
        entries = []
        for row in store.iter_records(source):
            if not row["geometry_json"]:
                continue
            geometry = json.loads(row["geometry_json"])
            bbox = geometry_bbox(geometry)
            if bbox:
                entries.append((bbox, geometry, json.loads(row["properties_json"])))
        indexes[layer] = entries

    counts = {layer: 0 for layer in layer_sources}
    counts["sem_ponto"] = 0
    counts["fora_das_malhas"] = 0
    match_cache: Dict[Tuple[float, float], Dict[str, List[Dict[str, Any]]]] = {}
    for record in records:
        properties = record["properties"]
        geometry = record.get("geometry")
        if not geometry or geometry.get("type") != "Point" or len(geometry.get("coordinates", [])) < 2:
            properties["contexto_territorial_status"] = "sem_ponto_para_cruzamento"
            counts["sem_ponto"] += 1
            continue
        point = (float(geometry["coordinates"][0]), float(geometry["coordinates"][1]))
        matches = match_cache.get(point)
        if matches is None:
            matches = {}
            for layer, entries in indexes.items():
                selected = []
                for bbox, polygon, layer_properties in entries:
                    if bbox[0] <= point[0] <= bbox[2] and bbox[1] <= point[1] <= bbox[3] and point_in_geometry(point, polygon):
                        selected.append(layer_properties)
                matches[layer] = selected
            match_cache[point] = matches
        for layer, selected in matches.items():
            if selected:
                counts[layer] += 1

        if any(matches.values()):
            properties["contexto_territorial_status"] = "cruzado_com_camadas_publicas"
        else:
            properties["contexto_territorial_status"] = "ponto_fora_das_malhas_publicadas"
            counts["fora_das_malhas"] += 1
        properties["contexto_territorial_baseado_em"] = properties.get("precisao_geometria")
        properties["fonte_contexto_territorial"] = "GeoMogi - Prefeitura de Mogi das Cruzes"
        properties["bairro_contexto"] = sorted({safe_text(item.get("texto")) for item in matches["bairro"] if safe_text(item.get("texto"))})
        properties["distrito_contexto"] = sorted({safe_text(item.get("texto")) for item in matches["distrito"] if safe_text(item.get("texto"))})
        properties["macrozona_siglas"] = sorted({safe_text(item.get("sigla")) for item in matches["macrozona"] if safe_text(item.get("sigla"))})
        properties["macrozona_contexto"] = sorted({safe_text(item.get("texto")) for item in matches["macrozona"] if safe_text(item.get("texto"))})
        zone_details = []
        for item in matches["zoneamento"]:
            detail = {
                name: value
                for name, value in item.items()
                if name not in {"cidade", "fonte", "camada", "texto", "id_fonte", "precisao_geometria", "id"}
                and value not in (None, "")
            }
            if detail and detail not in zone_details:
                zone_details.append(detail)
        properties["zoneamento_codigos"] = sorted({safe_text(item.get("zona")) for item in matches["zoneamento"] if safe_text(item.get("zona"))})
        properties["zoneamento_descricoes"] = sorted({safe_text(item.get("descricao")) for item in matches["zoneamento"] if safe_text(item.get("descricao"))})
        properties["zoneamento_detalhes"] = zone_details
    return counts


def export_mogi_canonical(store: CheckpointStore, folder: Path) -> Dict[str, int]:
    """Exporta uma visão por id_local para navegação territorial e agrupamento."""
    records = build_mogi_canonical_records(store)
    context_counts = apply_mogi_territorial_context(records, store)
    json_path = folder / "locais_agrupados.json"
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(
            [
                {
                    "id": record["id"],
                    **record["properties"],
                    **({"geometry": record["geometry"]} if record["geometry"] else {}),
                }
                for record in records
            ],
            handle,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        handle.write("\n")

    geo_count = 0
    geojson_path = folder / "locais_agrupados.geojson"
    with geojson_path.open("w", encoding="utf-8") as handle:
        handle.write('{"type":"FeatureCollection","features":[\n')
        first = True
        for record in records:
            if record["geometry"] is None:
                continue
            if not first:
                handle.write(",\n")
            json.dump(
                {
                    "type": "Feature",
                    "id": record["id"],
                    "geometry": record["geometry"],
                    "properties": record["properties"],
                },
                handle,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            first = False
            geo_count += 1
        handle.write("\n]}\n")

    preferred_fields = [
        "id", "cidade", "id_local", "id_local_normalizado", "quantidade_cadastros_no_local",
        "quantidade_cadastros_ativos", "quantidade_cadastros_com_valor_venal",
        "logradouros_encontrados", "numeros_encontrados", "complementos_encontrados",
        "tipos_cadastrais_encontrados", "usos_imovel_encontrados", "precisao_geometria",
        "metodo_georreferenciamento",
        "bairro_contexto", "distrito_contexto", "macrozona_siglas", "macrozona_contexto",
        "zoneamento_codigos", "zoneamento_descricoes", "zoneamento_detalhes",
        "contexto_territorial_status", "contexto_territorial_baseado_em", "fonte_contexto_territorial",
    ]
    all_fields = {name for record in records for name in record["properties"]}
    fields = preferred_fields + sorted(all_fields - set(preferred_fields))
    csv_path = folder / "locais_agrupados.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            row = {"id": record["id"], **record["properties"]}
            for name, value in list(row.items()):
                if isinstance(value, list):
                    row[name] = (
                        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                        if any(isinstance(item, (dict, list)) for item in value)
                        else ";".join(str(item) for item in value)
                    )
                elif isinstance(value, dict):
                    row[name] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            writer.writerow(row)
    return {
        "ids_locais": len(records),
        "georreferenciadas": geo_count,
        "sem_geometria": len(records) - geo_count,
        **{f"contexto_{name}": value for name, value in context_counts.items()},
    }


def export_city_results(store: CheckpointStore, results_root: Path) -> Dict[str, Dict[str, int]]:
    """Gera pastas independentes para Mogi, Itanhaém e a visão consolidada."""
    definitions = {
        "mogi": "Mogi das Cruzes",
        "itanhaem": "Itanhaém",
        "consolidado": None,
    }
    totals: Dict[str, Dict[str, int]] = {}
    generated_at = datetime.now(timezone.utc).isoformat()
    for slug, city in definitions.items():
        folder = results_root / slug
        folder.mkdir(parents=True, exist_ok=True)
        counts = {
            "json": export_json(store, folder / "dados_territoriais.json", city),
            "geojson": export_geojson(store, folder / "mapa_territorial.geojson", city),
            "csv": export_csv(store, folder / "dados_territoriais.csv", city),
        }
        if slug == "mogi":
            counts["cadastros_individuais"] = export_mogi_cadastros_csv(
                store, folder / "cadastros_individuais.csv"
            )
            counts["valores_venais_por_cadastro"] = export_mogi_iptu_venal_csv(
                store, folder / "valores_venais_por_cadastro.csv", latest_only=True
            )
            counts["historico_valores_venais_por_cadastro"] = export_mogi_iptu_venal_csv(
                store, folder / "historico_valores_venais_por_cadastro.csv"
            )
            for obsolete_name in (
                "valores_venais_iptu.csv", "valores_venais_por_inscricao.csv",
                "historico_valores_venais.csv", "imoveis_por_inscricao.csv",
                "imoveis_por_inscricao.json", "imoveis_por_inscricao.geojson",
            ):
                obsolete_path = folder / obsolete_name
                if obsolete_path.exists():
                    obsolete_path.unlink()
            canonical_counts = export_mogi_canonical(store, folder)
            counts.update({f"locais_{name}": value for name, value in canonical_counts.items()})
        manifest = {
            "cidade": city or "Mogi das Cruzes e Itanhaém",
            "gerado_em": generated_at,
            "arquivos": {
                "dados": "dados_territoriais.json",
                "mapa": "mapa_territorial.geojson",
                "planilha": "dados_territoriais.csv",
                **(
                    {
                        "cadastros_individuais": "cadastros_individuais.csv",
                        "valores_venais_por_cadastro": "valores_venais_por_cadastro.csv",
                        "historico_valores_venais_por_cadastro": "historico_valores_venais_por_cadastro.csv",
                        "locais_agrupados_csv": "locais_agrupados.csv",
                        "locais_agrupados_json": "locais_agrupados.json",
                        "locais_agrupados_mapa": "locais_agrupados.geojson",
                    }
                    if slug == "mogi"
                    else {}
                ),
            },
            "contagens": counts,
        }
        (folder / "manifesto.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        totals[slug] = counts
    return totals


def collection_statistics(store: CheckpointStore) -> Dict[str, int]:
    """Resume linhas e inscrições distintas para evitar interpretações ambíguas."""
    queries = {
        "mogi_linhas_cadastro": (
            "SELECT COUNT(*) FROM records WHERE source='mogi_cadastro_imobiliario_publico'"
        ),
        "mogi_cadastros_individuais": (
            "SELECT COUNT(DISTINCT json_extract(properties_json, '$.cadastro_id_normalizado')) "
            "FROM records WHERE source='mogi_cadastro_imobiliario_publico'"
        ),
        "mogi_ids_locais_distintos": (
            "SELECT COUNT(DISTINCT json_extract(properties_json, '$.id_local')) "
            "FROM records WHERE source='mogi_cadastro_imobiliario_publico'"
        ),
        "mogi_cadastros_com_valor_venal": (
            "SELECT COUNT(DISTINCT cadastro_id_normalizado) FROM mogi_cadastro_valor_venal"
        ),
        "mogi_cadastros_com_valor_venal_2024": (
            "SELECT COUNT(DISTINCT cadastro_id_normalizado) FROM mogi_cadastro_valor_venal WHERE exercicio=2024"
        ),
        "mogi_cadastros_com_valor_de_itbi": (
            "SELECT COUNT(DISTINCT cadastro_id_normalizado) FROM mogi_cadastro_valor_venal WHERE tipo_fonte='itbi'"
        ),
        "mogi_linhas_vinculadas_ao_valor_venal": (
            "SELECT COUNT(*) FROM records WHERE source='mogi_cadastro_imobiliario_publico' "
            "AND json_extract(properties_json, '$.valor_venal') IS NOT NULL"
        ),
        "itanhaem_feicoes_arruamento": (
            "SELECT COUNT(*) FROM records WHERE source='itanhaem_arruamento_publico'"
        ),
        "mogi_inscricoes_sem_geometria": (
            "SELECT COUNT(*) FROM ("
            "SELECT replace(replace(json_extract(properties_json, '$.inscricao_imobiliaria'), '-', ''), '.', '') chave "
            "FROM records WHERE source='mogi_cadastro_imobiliario_publico' GROUP BY chave "
            "HAVING SUM(CASE WHEN geometry_json IS NOT NULL THEN 1 ELSE 0 END)=0)"
        ),
        "certidoes_venais_importadas": "SELECT COUNT(*) FROM venal_validations",
        "situacoes_fiscais_importadas": "SELECT COUNT(*) FROM fiscal_status",
    }
    return {name: int(store.conn.execute(sql).fetchone()[0]) for name, sql in queries.items()}


def export_venal_validations_csv(
    store: CheckpointStore, output: Path, city: Optional[str] = None
) -> int:
    """Exporta somente metadados venais aceitos, incluindo o resultado do vínculo territorial."""
    fields = [
        "cidade", "inscricao_imobiliaria", "exercicio", "valor_venal", "moeda",
        "numero_certidao", "data_emissao", "arquivo_origem", "linha_origem",
        "cadastro_publico_encontrado", "registros_publicos_vinculados", "importado_em",
    ]
    public_index = public_cadastre_index(store)
    count = 0
    with output.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in store.iter_venal_validations():
            if city is not None and row["cidade"] != city:
                continue
            records = public_index.get((row["cidade"], row["inscricao_normalizada"]), [])
            writer.writerow(
                {
                    "cidade": row["cidade"],
                    "inscricao_imobiliaria": row["inscricao_imobiliaria"],
                    "exercicio": row["exercicio"],
                    "valor_venal": row["valor_venal"],
                    "moeda": row["moeda"],
                    "numero_certidao": row["numero_certidao"],
                    "data_emissao": row["data_emissao"],
                    "arquivo_origem": row["arquivo_origem"],
                    "linha_origem": row["linha_origem"],
                    "cadastro_publico_encontrado": bool(records),
                    "registros_publicos_vinculados": ";".join(records),
                    "importado_em": row["imported_at"],
                }
            )
            count += 1
    return count


def export_fiscal_status_csv(
    store: CheckpointStore, output: Path, city: Optional[str] = None
) -> int:
    """Exporta o status fiscal importado com indicação de vínculo ao cadastro público."""
    fields = [
        "cidade", "inscricao_imobiliaria", "competencia", "possui_pendencia",
        "status_pendencia", "valor_total_pendencia", "quantidade_pendencias",
        "fonte_consulta", "data_consulta", "numero_documento", "arquivo_origem",
        "linha_origem", "cadastro_publico_encontrado", "registros_publicos_vinculados",
        "importado_em",
    ]
    public_index = public_cadastre_index(store)
    count = 0
    with output.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in store.iter_fiscal_status():
            if city is not None and row["cidade"] != city:
                continue
            records = public_index.get((row["cidade"], row["inscricao_normalizada"]), [])
            writer.writerow(
                {
                    "cidade": row["cidade"],
                    "inscricao_imobiliaria": row["inscricao_imobiliaria"],
                    "competencia": row["competencia"],
                    "possui_pendencia": bool(row["possui_pendencia"]),
                    "status_pendencia": row["status_pendencia"],
                    "valor_total_pendencia": row["valor_total_pendencia"],
                    "quantidade_pendencias": row["quantidade_pendencias"],
                    "fonte_consulta": row["fonte_consulta"],
                    "data_consulta": row["data_consulta"],
                    "numero_documento": row["numero_documento"],
                    "arquivo_origem": row["arquivo_origem"],
                    "linha_origem": row["linha_origem"],
                    "cadastro_publico_encontrado": bool(records),
                    "registros_publicos_vinculados": ";".join(records),
                    "importado_em": row["imported_at"],
                }
            )
            count += 1
    return count


# -------------------------- CLI --------------------------

def parse_args() -> argparse.Namespace:
    """Define a interface de linha de comando usada para as coletas e exportações."""
    parser = argparse.ArgumentParser(
        description="Extrai cadastro territorial público de Itanhaém e Mogi das Cruzes."
    )
    parser.add_argument(
        "cidade", choices=("itanhaem", "mogi", "contexto-mogi", "ambos", "export", "importar-venal", "importar-fiscal"),
        help="Fonte a processar ou apenas 'export' para regenerar arquivos do checkpoint.",
    )
    parser.add_argument("--year", type=int, default=2024, help="Exercício do CSV de Mogi (padrão: 2024).")
    parser.add_argument(
        "--output-dir", type=Path, default=Path("data/atual"),
        help="Diretório da operação atual; resultados são separados por cidade dentro dele.",
    )
    parser.add_argument("--limit-tiles", type=int, default=None, help="Limita a quantidade de tiles de Itanhaém.")
    parser.add_argument("--tile", default=None, help="Processa somente um tile no formato z/x/y.")
    parser.add_argument(
        "--mogi-full",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--limit-mogi-resources",
        type=int,
        default=None,
        help="Limita o número de partes de Mogi processadas na execução.",
    )
    parser.add_argument(
        "--mogi-historico-desde",
        type=int,
        default=None,
        help="Completa lacunas venais com IPTU/ITBI históricos desde o ano informado.",
    )
    parser.add_argument(
        "--venal-file",
        type=Path,
        help="CSV/TXT ou JSON local com inscrições e valores venais de certidões já emitidas legitimamente.",
    )
    parser.add_argument(
        "--venal-cidade",
        choices=tuple(VENAL_CITY_LABELS),
        help="Município das certidões importadas; obrigatório em importar-venal.",
    )
    parser.add_argument(
        "--fiscal-file",
        type=Path,
        help="CSV/TXT ou JSON local com status agregado de pendência por inscrição, sem boleto.",
    )
    parser.add_argument(
        "--fiscal-cidade",
        choices=tuple(VENAL_CITY_LABELS),
        help="Município da situação fiscal importada; obrigatório em importar-fiscal.",
    )
    parser.add_argument("--force", action="store_true", help="Reprocessa recursos já concluídos.")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return parser.parse_args()


def main() -> int:
    """Orquestra a fonte escolhida, aplica vínculos venais e produz todas as saídas locais."""
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s | %(levelname)s | %(message)s")

    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    store = CheckpointStore(out / "checkpoint.sqlite")
    client = PersistentHttpClient(out / "session-cookies.txt")

    try:
        if args.mogi_historico_desde is not None and not (2016 <= args.mogi_historico_desde <= args.year):
            raise ValueError("--mogi-historico-desde deve estar entre 2016 e o exercício principal.")
        if args.cidade == "importar-venal":
            if not args.venal_file or not args.venal_cidade:
                raise ValueError("importar-venal exige --venal-file e --venal-cidade.")
            result = import_venal_validations(store, args.venal_file, args.venal_cidade)
            logging.info("Certidões venais importadas: %s", result)

        if args.cidade == "importar-fiscal":
            if not args.fiscal_file or not args.fiscal_cidade:
                raise ValueError("importar-fiscal exige --fiscal-file e --fiscal-cidade.")
            result = import_fiscal_status(store, args.fiscal_file, args.fiscal_cidade)
            logging.info("Situações fiscais importadas: %s", result)

        if args.cidade in ("itanhaem", "ambos"):
            only_tile = None
            if args.tile:
                try:
                    only_tile = tuple(int(part) for part in args.tile.split("/"))
                    if len(only_tile) != 3:
                        raise ValueError
                except ValueError:
                    raise ValueError("--tile deve estar no formato z/x/y.")
            result = ItanhaemPublicExtractor(client, store).run(args.limit_tiles, args.force, only_tile)
            logging.info("Itanhaém concluído: %s", result)

        if args.cidade in ("mogi", "ambos"):
            if args.force:
                removed_venal = store.clear_mogi_cadastro_venal()
                logging.info("Mogi: %s valores da versão anterior removidos antes da reimportação.", removed_venal)
            # Camadas geométricas de referência territorial (GeoMogi)
            quadras = MogiQuadrasExtractor(client, store).run(args.force)
            logging.info("Mogi: quadras concluídas: %s", quadras)
            logradouros = MogiLogradourosExtractor(client, store).run(args.force)
            logging.info("Mogi: logradouros concluídos: %s", logradouros)

            # Camadas públicas de contexto territorial
            extra_layers = [k for k in GEOMOGI_LAYERS if k not in ("quadra", "logradouro")]
            for layer in extra_layers:
                try:
                    r = MogiGeoLayerExtractor(layer, client, store).run(args.force)
                    logging.info("GeoMogi/%s concluído: %s", layer, r)
                except Exception as exc:
                    logging.warning("GeoMogi/%s falhou (não crítico): %s", layer, exc)

            # Linhas do cadastro imobiliário publicadas no portal municipal
            result = MogiPublicExtractor(client, store).run(
                args.year,
                args.force,
                full=True,
                limit_resources=args.limit_mogi_resources,
            )
            logging.info("Mogi: cadastro imobiliário concluído: %s", result)
            iptu_venal = MogiIptuVenalExtractor(client, store).run(
                args.year,
                args.force,
                limit_resources=args.limit_mogi_resources,
            )
            logging.info("Mogi: valores venais da base IPTU concluídos: %s", iptu_venal)
            itbi_venal = MogiItbiVenalExtractor(client, store).run(args.year, args.force)
            logging.info("Mogi: valores venais da base IPTU/ITBI concluídos: %s", itbi_venal)
            if args.mogi_historico_desde is not None:
                for historical_year in range(args.year - 1, args.mogi_historico_desde - 1, -1):
                    historical_iptu = MogiIptuVenalExtractor(client, store).run(
                        historical_year,
                        args.force,
                        limit_resources=args.limit_mogi_resources,
                    )
                    logging.info("Mogi: IPTU histórico %s concluído: %s", historical_year, historical_iptu)
                    historical_itbi = MogiItbiVenalExtractor(client, store).run(
                        historical_year, args.force
                    )
                    logging.info("Mogi: IPTU/ITBI histórico %s concluído: %s", historical_year, historical_itbi)
            georeference = georeference_mogi_records(store)
            logging.info("Mogi: georreferenciamento concluído: %s", georeference)

        if args.cidade == "contexto-mogi":
            for layer in ("bairro", "zoneamento", "macrozona", "distrito"):
                result = MogiGeoLayerExtractor(layer, client, store).run(force=True)
                logging.info("GeoMogi/%s atualizado: %s", layer, result)

        iptu_vinculacao = apply_mogi_iptu_venal(store)
        if iptu_vinculacao["valores_disponiveis"]:
            logging.info("Valores venais públicos de Mogi vinculados: %s", iptu_vinculacao)

        venal_vinculacao = apply_venal_validations(store)
        if venal_vinculacao["validacoes"]:
            logging.info("Certidões venais vinculadas: %s", venal_vinculacao)

        fiscal_vinculacao = apply_fiscal_status(store)
        if fiscal_vinculacao["situacoes"]:
            logging.info("Situações fiscais vinculadas: %s", fiscal_vinculacao)

        # A saída ativa fica separada por cidade; o consolidado é gerado à parte.
        results_root = out / "resultados"
        organized_counts = export_city_results(store, results_root)
        n_venal_mogi = export_venal_validations_csv(
            store, results_root / "mogi" / "certidoes_venais_importadas.csv", "Mogi das Cruzes"
        )
        n_venal_itanhaem = export_venal_validations_csv(
            store, results_root / "itanhaem" / "certidoes_venais_importadas.csv", "Itanhaém"
        )
        n_fiscal_mogi = export_fiscal_status_csv(
            store, results_root / "mogi" / "situacao_fiscal_importada.csv", "Mogi das Cruzes"
        )
        n_fiscal_itanhaem = export_fiscal_status_csv(
            store, results_root / "itanhaem" / "situacao_fiscal_importada.csv", "Itanhaém"
        )
        manifest = {
            "gerado_em": datetime.now(timezone.utc).isoformat(),
            "resumo_por_fonte": store.summary(),
            "estatisticas": collection_statistics(store),
            "arquivos": {
                "mogi": "resultados/mogi",
                "itanhaem": "resultados/itanhaem",
                "consolidado": "resultados/consolidado",
                "checkpoint": "checkpoint.sqlite",
            },
            "contagens": organized_counts,
            "importacoes_locais": {
                "mogi": {"certidoes_venais": n_venal_mogi, "situacoes_fiscais": n_fiscal_mogi},
                "itanhaem": {"certidoes_venais": n_venal_itanhaem, "situacoes_fiscais": n_fiscal_itanhaem},
            },
            "escopo": "Mogi reúne cadastro, georreferência territorial e valor venal da base pública de IPTU. Itanhaém reúne a camada pública de arruamento disponível. Certidões e situações fiscais importadas são mantidas em arquivos próprios e não são confundidas com o cadastro público.",
        }
        (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        logging.info("Resultados separados por cidade. %s", json.dumps(organized_counts, ensure_ascii=False))
        return 0
    except KeyboardInterrupt:
        logging.warning("Execução interrompida. O checkpoint foi preservado; execute o mesmo comando para retomar.")
        return 130
    except Exception as exc:
        logging.exception("Execução interrompida por erro: %s", exc)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
