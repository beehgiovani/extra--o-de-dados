"use strict";

const state = {
  page: 1,
  pages: 1,
  map: null,
  markerLayer: null,
  mapRenderer: null,
};

// Mantém as buscas de elementos curtas sem esconder o uso normal da API do navegador.
const byId = (id) => document.getElementById(id);
const numberFormat = new Intl.NumberFormat("pt-BR");
const moneyFormat = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
const decimalFormat = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 2 });

// Escapa qualquer texto vindo da base antes de inseri-lo em trechos de HTML.
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

// Padroniza campos que podem chegar como texto único, lista ou valor ausente.
function asArray(value) {
  if (value === null || value === undefined || value === "") return [];
  return Array.isArray(value) ? value : [value];
}

// Apresenta listas e valores simples com uma mensagem clara quando não há dado.
function showValue(value, fallback = "Não informado") {
  const values = asArray(value).filter((item) => item !== "");
  return values.length ? values.join("; ") : fallback;
}

// Formata valores monetários sem transformar ausência em zero.
function formatMoney(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? moneyFormat.format(numeric) : "Não disponível";
}

// Formata áreas preservando a diferença entre zero e campo não informado.
function formatArea(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? `${decimalFormat.format(numeric)} m²` : "Não informado";
}

// Traduz códigos internos para os mesmos rótulos usados nas fichas da interface.
function humanize(value) {
  const labels = {
    centro_da_quadra: "Centro da quadra fiscal",
    centro_do_logradouro: "Centro do logradouro",
    centro_do_logradouro_aproximado: "Logradouro por correspondência aproximada",
    sem_geometria: "Sem geometria",
    disponivel_exercicio_principal: "Disponível no exercício principal",
    disponivel_historico: "Disponível somente no histórico",
    indisponivel_cadastro_desativado: "Cadastro desativado sem valor publicado",
    indisponivel_cadastro_imune: "Cadastro imune sem valor publicado",
    indisponivel_cadastro_isento: "Cadastro isento sem valor publicado",
    indisponivel_nas_bases_publicas_2016_2024: "Ausente nas bases públicas de 2016 a 2024",
    cruzado_com_camadas_publicas: "Cruzado com as camadas territoriais",
    ponto_fora_das_malhas_publicadas: "Ponto fora das malhas publicadas",
    sem_ponto_para_cruzamento: "Sem ponto para cruzamento",
  };
  return labels[value] || String(value || "Não informado").replaceAll("_", " ");
}

// Centraliza as chamadas à API e devolve a mensagem real enviada pelo servidor.
async function getJson(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.erro || `Falha na consulta (${response.status}).`);
  }
  return response.json();
}

// Carrega os totais e as opções de filtro calculados a partir do índice local.
async function loadSummary() {
  const payload = await getJson("/api/resumo");
  byId("metricTotal").textContent = numberFormat.format(payload.resumo.cadastros);
  byId("metricVenal").textContent = numberFormat.format(payload.resumo.com_valor_venal);
  byId("metricGeo").textContent = numberFormat.format(payload.resumo.georreferenciados);
  byId("metricContext").textContent = numberFormat.format(payload.resumo.com_contexto_territorial);
  populateSelect("filterBairro", payload.opcoes.bairros);
  populateSelect("filterZona", payload.opcoes.zonas);
  populateSelect("filterSituacao", payload.opcoes.situacoes.map((value) => ({ value, label: humanize(value) })));
  populateSelect("filterPrecisao", payload.opcoes.precisoes.map((value) => ({ value, label: humanize(value) })));
}

// Aceita texto simples ou objetos quando o rótulo precisa ser mais legível que o valor salvo.
function populateSelect(id, values) {
  const select = byId(id);
  values.forEach((item) => {
    const value = typeof item === "object" ? item.value : item;
    const label = typeof item === "object" ? item.label : item;
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.appendChild(option);
  });
}

// Reúne busca, filtros e paginação em um único conjunto de parâmetros.
function currentParams() {
  const params = new URLSearchParams({
    q: byId("searchInput").value.trim(),
    bairro: byId("filterBairro").value,
    zona: byId("filterZona").value,
    situacao: byId("filterSituacao").value,
    precisao: byId("filterPrecisao").value,
    pagina: String(state.page),
    limite: "30",
  });
  return params;
}

// Substitui a lista por cartões neutros enquanto a consulta está em andamento.
function loadingResults() {
  const list = byId("resultList");
  list.replaceChildren();
  for (let index = 0; index < 5; index += 1) {
    list.appendChild(byId("loadingTemplate").content.cloneNode(true));
  }
}

// Monta um resultado curto; a ficha completa continua sendo carregada sob demanda.
function resultCard(record) {
  const card = document.createElement("button");
  card.type = "button";
  card.className = "result-card";
  card.dataset.cadastro = record.cadastro_id_normalizado;
  const address = [record.logradouro, record.numero].filter(Boolean).join(", ") || "Endereço não informado";
  const context = [showValue(record.bairro_contexto, "Bairro não identificado"), showValue(record.zoneamento_codigos, "Zona não identificada")].join(" · ");
  const precisionTag = record.coordinates ? humanize(record.precisao_geometria) : "Sem ponto";
  card.innerHTML = `
    <div class="result-top">
      <span class="result-inscription">CADASTRO ${escapeHtml(record.cadastro_id)} · LOCAL ${escapeHtml(record.id_local)}</span>
      <strong class="result-value">${escapeHtml(formatMoney(record.valor_venal))}</strong>
    </div>
    <p class="result-address">${escapeHtml(address)}</p>
    <p class="result-context">${escapeHtml(context)}</p>
    <div class="tags">
      <span class="tag">${escapeHtml(showValue(record.uso_imovel, "Uso não informado"))}</span>
      <span class="tag tag-muted">${escapeHtml(precisionTag)}</span>
    </div>`;
  card.addEventListener("click", () => openDetail(record.cadastro_id_normalizado));
  return card;
}

// Atualiza somente a lista paginada e seus controles de navegação.
function renderResults(payload) {
  const list = byId("resultList");
  list.replaceChildren();
  if (!payload.resultados.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "Nenhum cadastro corresponde aos filtros informados.";
    list.appendChild(empty);
  } else {
    payload.resultados.forEach((record) => list.appendChild(resultCard(record)));
  }
  state.pages = payload.paginas;
  byId("resultCount").textContent = `${numberFormat.format(payload.total)} cadastros encontrados`;
  byId("pageLabel").textContent = `Página ${payload.pagina} de ${payload.paginas}`;
  byId("previousPage").disabled = payload.pagina <= 1;
  byId("nextPage").disabled = payload.pagina >= payload.paginas;
}

// Pesquisa a lista e, quando necessário, atualiza também o conjunto completo do mapa.
async function runSearch(refreshMap = true) {
  loadingResults();
  try {
    const params = currentParams();
    const requests = [getJson(`/api/imoveis?${params}`)];
    if (refreshMap) {
      const mapParams = new URLSearchParams(params);
      mapParams.delete("pagina");
      mapParams.delete("limite");
      requests.push(getJson(`/api/pontos?${mapParams}`));
    }
    const [payload, mapPayload] = await Promise.all(requests);
    renderResults(payload);
    if (mapPayload) updateMap(mapPayload);
  } catch (error) {
    const list = byId("resultList");
    list.innerHTML = `<div class="empty-state error-state">${escapeHtml(error.message)}</div>`;
  }
}

// Prepara o Leaflet com dependências locais e usa a internet apenas no mapa-base.
function initializeMap() {
  if (!window.L) {
    byId("mapFallback").hidden = false;
    return;
  }
  state.map = L.map("map", { zoomControl: false }).setView([-23.522, -46.188], 11);
  L.control.zoom({ position: "bottomright" }).addTo(state.map);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(state.map);
  state.markerLayer = L.layerGroup().addTo(state.map);
  state.mapRenderer = L.canvas({ padding: .4 });
}

// Desenha uma marca por coordenada e informa quantos cadastros compartilham o ponto.
function updateMap(payload) {
  const points = Array.isArray(payload.pontos) ? payload.pontos : [];
  byId("mapCount").textContent = numberFormat.format(payload.total_pontos || 0);
  byId("mapCadastros").textContent = numberFormat.format(payload.cadastros_georreferenciados || 0);
  if (!state.map || !state.markerLayer) return;
  state.markerLayer.clearLayers();
  const bounds = [];
  points.forEach((point) => {
    const position = [Number(point.latitude), Number(point.longitude)];
    if (!position.every(Number.isFinite)) return;
    bounds.push(position);
    const amount = Number(point.cadastros) || 1;
    const radius = Math.min(14, 4 + Math.log10(amount + 1) * 2.6);
    const marker = L.circleMarker(position, {
      renderer: state.mapRenderer,
      radius,
      color: "#ffffff",
      weight: 1.5,
      fillColor: amount >= 100 ? "#b85c1e" : "#0e6a4f",
      fillOpacity: .86,
    });
    const cadastroLabel = amount === 1 ? "cadastro" : "cadastros";
    const localAmount = Number(point.ids_locais) || 0;
    const localLabel = localAmount === 1 ? "id_local" : "id_local distintos";
    marker.bindPopup(`<div class="map-popup"><strong>${numberFormat.format(amount)} ${cadastroLabel}</strong><p>${numberFormat.format(localAmount)} ${localLabel} nesta referência</p><p>${escapeHtml(point.logradouro_exemplo || "Endereço não informado")}</p><p>${escapeHtml(point.bairro_exemplo || "Bairro não identificado")}${point.zona_exemplo ? ` · ${escapeHtml(point.zona_exemplo)}` : ""}</p><small>${escapeHtml(humanize(point.precisao_exemplo))}</small></div>`);
    marker.addTo(state.markerLayer);
  });
  if (bounds.length === 1) state.map.setView(bounds[0], 15);
  if (bounds.length > 1) state.map.fitBounds(bounds, { padding: [40, 40], maxZoom: 15 });
}

// Gera uma célula da ficha mantendo rótulo e valor com a mesma estrutura visual.
function dataItem(label, value, wide = false) {
  return `<div class="data-item${wide ? " data-item-wide" : ""}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

// Mantém todas as seções da ficha com cabeçalho e espaçamento consistentes.
function section(title, content) {
  return `<section class="detail-section"><h3>${escapeHtml(title)}</h3>${content}</section>`;
}

// Mostra somente parâmetros urbanísticos realmente publicados para a zona encontrada.
function zoneDetails(details) {
  const labels = {
    zona: "Zona",
    descricao: "Descrição",
    txo: "Taxa de ocupação",
    tp: "Taxa de permeabilidade",
    camin: "Coeficiente mínimo",
    cab: "Coeficiente básico",
    cam: "Coeficiente máximo",
    gabarito: "Gabarito",
    zesp: "Zonas especiais",
    obs: "Observações",
    b1: "Uso B1",
    b2: "Uso B2",
    c1: "Uso C1",
    d1: "Uso D1",
    d2: "Uso D2",
    ret1: "Condição 1",
    ret2: "Condição 2",
  };
  if (!Array.isArray(details) || !details.length) return '<p class="empty-state">Sem parâmetros de zoneamento associados ao ponto.</p>';
  return details.map((detail) => {
    const rows = Object.entries(detail)
      .filter(([, value]) => value !== null && value !== "")
      .map(([key, value]) => `<div><dt>${escapeHtml(labels[key] || key)}</dt><dd>${escapeHtml(value)}</dd></div>`)
      .join("");
    return `<div class="zone-card"><h4>${escapeHtml(detail.zona || "Parâmetros publicados")}</h4><dl>${rows}</dl></div>`;
  }).join("");
}

// Resume o histórico venal em barras relativas sem alterar os valores originais.
function historyContent(history) {
  if (!history.length) return '<p class="empty-state">Não há valor venal no histórico público consultado.</p>';
  const maximum = Math.max(...history.map((row) => Number(row.valor_venal_total) || 0), 1);
  return `<div class="history-list">${history.map((row) => {
    const width = Math.max(2, ((Number(row.valor_venal_total) || 0) / maximum) * 100);
    return `<div class="history-row"><strong>${row.exercicio}</strong><div class="history-bar"><i style="width:${width.toFixed(2)}%"></i></div><span>${escapeHtml(formatMoney(row.valor_venal_total))}</span></div>`;
  }).join("")}</div>`;
}

// Compõe a ficha completa deixando explícita a precisão aproximada da georreferência.
function detailMarkup(record, history, group) {
  const precisionNotice = record.geometry
    ? `A localização usa ${humanize(record.precisao_geometria).toLowerCase()}. O bairro e o zoneamento descrevem esse ponto de referência, não o polígono exato do lote.`
    : "Este cadastro ainda não possui ponto geográfico para cruzamento territorial.";
  const cadastral = `<div class="data-grid">
    ${dataItem("Cadastro municipal", showValue(record.cadastro_id))}
    ${dataItem("ID local", showValue(record.id_local))}
    ${dataItem("Exercício cadastral", showValue(record.exercicio))}
    ${dataItem("Tipo cadastral", showValue(record.tipo), true)}
    ${dataItem("Uso do imóvel", showValue(record.uso_imovel))}
    ${dataItem("Complemento / unidade", showValue(record.complemento))}
    ${dataItem("Área do terreno", formatArea(record.area_terreno_m2))}
    ${dataItem("Área construída", formatArea(record.area_construcao_m2))}
    ${dataItem("Cadastros no mesmo id_local", numberFormat.format(Number(group?.quantidade_cadastros_no_local) || 0))}
    ${dataItem("Cadastros ativos no id_local", numberFormat.format(Number(group?.quantidade_cadastros_ativos) || 0))}
  </div>`;
  const venal = `<div class="data-grid">
    ${dataItem("Valor venal total", formatMoney(record.valor_venal))}
    ${dataItem("Exercício", showValue(record.exercicio_valor_venal))}
    ${dataItem("Terreno", formatMoney(record.valor_venal_terreno))}
    ${dataItem("Construção", formatMoney(record.valor_venal_construcao))}
    ${dataItem("Situação", humanize(record.situacao_valor_venal), true)}
    ${dataItem("Origem", showValue(record.fonte_valor_venal), true)}
    ${dataItem("Recurso", showValue(record.recurso_fonte_valor_venal), true)}
  </div>`;
  const territorial = `<div class="data-grid">
    ${dataItem("Bairro", showValue(record.bairro_contexto))}
    ${dataItem("Distrito", showValue(record.distrito_contexto))}
    ${dataItem("Macrozona", `${showValue(record.macrozona_siglas)} — ${showValue(record.macrozona_contexto)}`)}
    ${dataItem("Zoneamento", `${showValue(record.zoneamento_codigos)} — ${showValue(record.zoneamento_descricoes)}`)}
    ${dataItem("Referência espacial", humanize(record.precisao_geometria), true)}
    ${dataItem("Método", humanize(record.metodo_georreferenciamento), true)}
    ${dataItem("Status do cruzamento", humanize(record.contexto_territorial_status), true)}
  </div>`;
  const audit = `<div class="data-grid">
    ${dataItem("Fonte cadastral", showValue(record.fonte), true)}
    ${dataItem("Cadastro normalizado", showValue(record.cadastro_id_normalizado))}
    ${dataItem("Chave da quadra", showValue(record.quadra_chave))}
    ${dataItem("Situação cadastral", showValue(record.situacao_cadastro))}
    ${dataItem("Zona fiscal", showValue(record.zona_fiscal))}
  </div>`;
  return `
    <div class="notice"><strong>Atenção:</strong><span>${escapeHtml(precisionNotice)}</span></div>
    ${section("Cadastro do imóvel", cadastral)}
    ${section("Valor venal mais recente", venal)}
    ${section("Histórico de valores venais", historyContent(history))}
    ${section("Contexto territorial", territorial)}
    ${section("Parâmetros de zoneamento publicados", zoneDetails(record.zoneamento_detalhes))}
    ${section("Origem e auditoria", audit)}`;
}

// Busca a ficha de um cadastro apenas quando o usuário decide conferi-lo.
async function openDetail(inscription) {
  const panel = byId("detailPanel");
  byId("detailBackdrop").hidden = false;
  panel.classList.add("open");
  panel.setAttribute("aria-hidden", "false");
  byId("detailContent").innerHTML = '<div class="loading-card"><span></span><span></span><span></span></div>';
  try {
    const payload = await getJson(`/api/imoveis/${encodeURIComponent(inscription)}`);
    const record = payload.imovel;
    byId("detailTitle").textContent = record.cadastro_id ? `Cadastro ${record.cadastro_id}` : "Cadastro imobiliário";
    byId("detailAddress").textContent = [record.logradouro, record.numero_imovel_quadra, record.complemento].filter(Boolean).join(", ") || "Endereço não informado";
    byId("detailContent").innerHTML = detailMarkup(record, payload.historico_valor_venal || [], payload.grupo_local || {});
  } catch (error) {
    byId("detailContent").innerHTML = `<div class="empty-state error-state">${escapeHtml(error.message)}</div>`;
  }
}

// Fecha a ficha e devolve o foco visual à lista e ao mapa.
function closeDetail() {
  byId("detailPanel").classList.remove("open");
  byId("detailPanel").setAttribute("aria-hidden", "true");
  byId("detailBackdrop").hidden = true;
}

// Restaura os filtros e volta para a primeira página da base.
function clearFilters() {
  byId("searchForm").reset();
  state.page = 1;
  runSearch();
}

// Concentra os eventos da tela para evitar comportamento espalhado pelo arquivo.
function bindEvents() {
  byId("searchForm").addEventListener("submit", (event) => {
    event.preventDefault();
    state.page = 1;
    runSearch();
  });
  ["filterBairro", "filterZona", "filterSituacao", "filterPrecisao"].forEach((id) => {
    byId(id).addEventListener("change", () => {
      state.page = 1;
      runSearch();
    });
  });
  byId("clearFilters").addEventListener("click", clearFilters);
  byId("previousPage").addEventListener("click", () => {
    if (state.page > 1) { state.page -= 1; runSearch(false); }
  });
  byId("nextPage").addEventListener("click", () => {
    if (state.page < state.pages) { state.page += 1; runSearch(false); }
  });
  byId("closeDetail").addEventListener("click", closeDetail);
  byId("detailBackdrop").addEventListener("click", closeDetail);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeDetail();
  });
}

// Inicializa a interface na ordem necessária: eventos, mapa, resumo e primeira busca.
async function start() {
  bindEvents();
  initializeMap();
  try {
    await loadSummary();
    await runSearch();
  } catch (error) {
    byId("resultList").innerHTML = `<div class="empty-state error-state">${escapeHtml(error.message)}</div>`;
  }
}

start();
