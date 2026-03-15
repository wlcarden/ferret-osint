"""Cytoscape.js interactive graph exporter.

Generates a self-contained HTML file with an interactive force-directed
graph visualization of the entity/relationship data in the graph store.
"""

import json


# Keys that are structural metadata, not display properties.
_META_KEYS = frozenset({
    "id", "entity_type", "label", "sources",
    "source", "target", "relation_type",
})

# Properties too large or noisy for graph JSON payload.
_SKIP_PROPS = frozenset({"raw_data", "extracted_ids"})


class GraphExporter:
    """Generates a self-contained HTML file with Cytoscape.js graph."""

    async def export(
        self,
        store,
        investigation_name: str = "",
        investigation_id: int | None = None,
    ) -> str:
        """Read entities/relationships from store and render HTML.

        If investigation_id is provided, only entities linked to that
        investigation (and edges between them) are included.
        """
        if investigation_id is not None:
            entity_rows = await store.query(f"inv:{investigation_id}:all_nodes")
            rel_rows = await store.query(f"inv:{investigation_id}:all_edges")
        else:
            entity_rows = await store.query("all_nodes")
            rel_rows = await store.query("all_edges")
        return self.export_from_data(entity_rows, rel_rows, investigation_name)

    def export_from_data(
        self,
        entity_rows: list[dict],
        rel_rows: list[dict],
        title: str = "",
    ) -> str:
        """Build HTML from raw store dicts (sync, testable without DB)."""
        nodes = []
        source_tools = set()

        for row in entity_rows:
            tools = sorted({
                s.get("tool", "unknown") for s in row.get("sources", [])
            })
            source_tools.update(tools)

            props = {
                k: v for k, v in row.items()
                if k not in _META_KEYS and k not in _SKIP_PROPS
                and v not in (None, "", [], {})
            }

            nodes.append({
                "data": {
                    "id": row["id"],
                    "label": _trunc(row["label"], 28),
                    "fullLabel": row["label"],
                    "type": row["entity_type"],
                    "tools": tools,
                    "props": props,
                },
            })

        node_ids = {n["data"]["id"] for n in nodes}
        edges = []

        for row in rel_rows:
            src, tgt = row["source"], row["target"]
            if src not in node_ids or tgt not in node_ids:
                continue

            props = {
                k: v for k, v in row.items()
                if k not in _META_KEYS and k not in _SKIP_PROPS
                and v not in (None, "", [], {})
            }

            edges.append({
                "data": {
                    "source": src,
                    "target": tgt,
                    "type": row["relation_type"],
                    "props": props,
                },
            })

        graph_json = json.dumps(
            {"nodes": nodes, "edges": edges},
            separators=(",", ":"),
        )

        entity_types = sorted({n["data"]["type"] for n in nodes})
        rel_types = sorted({e["data"]["type"] for e in edges})

        html = _TEMPLATE
        html = html.replace("__GRAPH_DATA__", graph_json)
        html = html.replace("__ENTITY_TYPES__", json.dumps(entity_types))
        html = html.replace("__REL_TYPES__", json.dumps(rel_types))
        html = html.replace("__SOURCE_TOOLS__", json.dumps(sorted(source_tools)))
        html = html.replace("__TITLE__", _escape_html(title or "OSINT Investigation Graph"))
        return html


def _trunc(s: str, n: int) -> str:
    """Truncate string to n characters with ellipsis."""
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def _escape_html(s: str) -> str:
    """Escape HTML special characters in a string."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.30.4/cytoscape.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:#1e1e2e;color:#cdd6f4;overflow:hidden}

#app{display:grid;grid-template-columns:260px 1fr;grid-template-rows:44px 1fr;height:100vh;transition:grid-template-columns .2s}
#app.detail-open{grid-template-columns:260px 1fr 340px}

/* --- Top bar --- */
#topbar{grid-column:1/-1;background:#181825;border-bottom:1px solid #313244;display:flex;align-items:center;padding:0 16px;gap:10px}
#topbar h1{font-size:14px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:260px}
#topbar select,#topbar button{background:#313244;color:#cdd6f4;border:1px solid #45475a;border-radius:4px;padding:4px 10px;font-size:12px;cursor:pointer;white-space:nowrap}
#topbar select:hover,#topbar button:hover{background:#45475a}
#topbar button.active{background:#89b4fa33;border-color:#89b4fa;color:#89b4fa}
.tb-sep{width:1px;height:20px;background:#313244;flex-shrink:0}
.stats{font-size:12px;color:#6c7086;margin-left:auto;white-space:nowrap}
.shortcut{font-size:9px;color:#45475a;margin-left:2px}

/* --- Sidebar --- */
#sidebar{background:#181825;border-right:1px solid #313244;padding:10px;overflow-y:auto;font-size:13px;grid-column:1;grid-row:2}
.filter-header{display:flex;align-items:center;margin:14px 0 6px}
.filter-header:first-child{margin-top:4px}
.filter-header h3{font-size:10px;text-transform:uppercase;letter-spacing:.8px;color:#6c7086;user-select:none;flex:1}
.filter-toggle{font-size:9px;color:#585b70;cursor:pointer;padding:1px 4px;border-radius:2px}
.filter-toggle:hover{color:#89b4fa}
#search{width:100%;padding:6px 8px;background:#313244;border:1px solid #45475a;border-radius:4px;color:#cdd6f4;font-size:12px;outline:none;margin-bottom:4px}
#search:focus{border-color:#89b4fa}
#search::placeholder{color:#585b70}
.filter-item{display:flex;align-items:center;gap:6px;padding:2px 0;cursor:pointer;user-select:none}
.filter-item input{accent-color:#89b4fa;cursor:pointer}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block;flex-shrink:0}
.filter-item.edge-filter .dot{border-radius:2px;width:14px;height:4px}
.count{color:#585b70;font-size:11px;margin-left:auto}

/* --- Cytoscape canvas --- */
#cy{background:#1e1e2e;grid-column:2;grid-row:2;min-width:0;overflow:hidden}

/* --- Tooltip --- */
#tip{position:fixed;background:#313244;border:1px solid #45475a;border-radius:6px;padding:8px 10px;pointer-events:none;z-index:100;display:none;max-width:320px;font-size:11px;box-shadow:0 4px 12px #00000066}
#tip .tip-label{font-weight:600;font-size:12px;margin-bottom:4px;word-break:break-word}
#tip .tip-badge{display:inline-block;font-size:9px;padding:1px 6px;border-radius:3px;font-weight:600;text-transform:uppercase;margin-bottom:4px}
#tip .tip-row{color:#a6adc8;font-size:10px;margin-top:2px}
#tip .tip-row span{color:#cdd6f4}
#tip .tip-id{color:#585b70;font-size:9px;margin-top:3px;word-break:break-all}

/* --- Context menu --- */
#ctx-menu{position:fixed;background:#313244;border:1px solid #45475a;border-radius:6px;padding:4px 0;z-index:200;display:none;min-width:180px;box-shadow:0 4px 16px #000000aa;font-size:12px}
#ctx-menu .ctx-item{padding:6px 14px;cursor:pointer;display:flex;align-items:center;gap:8px}
#ctx-menu .ctx-item:hover{background:#45475a}
#ctx-menu .ctx-item .ctx-key{margin-left:auto;font-size:10px;color:#585b70}
#ctx-menu .ctx-sep{height:1px;background:#45475a;margin:3px 0}

/* --- Detail panel --- */
#detail{background:#181825;border-left:1px solid #313244;padding:14px;overflow-y:auto;display:none;font-size:13px;grid-column:3;grid-row:2}
#app.detail-open #detail{display:block}
#detail-header{display:flex;align-items:start;gap:8px;margin-bottom:6px}
#detail-header h2{font-size:14px;font-weight:600;word-break:break-word;flex:1}
#close-detail{background:none;border:none;color:#6c7086;font-size:18px;cursor:pointer;padding:0 4px;line-height:1}
#close-detail:hover{color:#cdd6f4}
#detail-id{font-size:10px;color:#585b70;word-break:break-all;margin-bottom:8px}
.badge{display:inline-block;font-size:10px;padding:2px 8px;border-radius:3px;font-weight:600;text-transform:uppercase;margin-bottom:8px}
.degree-badge{display:inline-block;font-size:10px;padding:2px 8px;border-radius:3px;background:#313244;color:#a6adc8;margin-left:6px;margin-bottom:8px}
#detail h3{font-size:10px;text-transform:uppercase;letter-spacing:.8px;color:#6c7086;margin:12px 0 6px;border-top:1px solid #313244;padding-top:10px}
#detail h3:first-of-type{border-top:none;padding-top:0}
#detail table{width:100%;border-collapse:collapse}
#detail th{text-align:left;color:#6c7086;padding:3px 8px 3px 0;font-weight:400;vertical-align:top;white-space:nowrap;font-size:11px}
#detail td{padding:3px 0;word-break:break-all;font-size:12px}
#detail td a{color:#89b4fa;text-decoration:none}
#detail td a:hover{text-decoration:underline}
.conn-group-header{font-size:10px;color:#585b70;padding:6px 0 3px;text-transform:uppercase;letter-spacing:.5px;display:flex;align-items:center;gap:6px}
.conn-group-header .dot{width:8px;height:3px;border-radius:1px}
.conn-item{padding:4px 6px;display:flex;align-items:center;gap:6px;cursor:pointer;border-radius:4px;margin:1px 0}
.conn-item:hover{background:#31324488}
.conn-dir{font-size:14px;color:#585b70;width:16px;text-align:center;flex-shrink:0}
.conn-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.conn-label{font-size:12px;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.conn-type-tag{font-size:9px;color:#585b70;background:#1e1e2e;padding:1px 5px;border-radius:2px;flex-shrink:0}
.tool-tag{display:inline-block;font-size:10px;padding:2px 6px;background:#313244;border-radius:3px;margin:2px 2px 2px 0}

/* --- Path info bar --- */
#path-info{display:none;position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:#313244;border:1px solid #f9e2af;border-radius:8px;padding:8px 16px;z-index:100;font-size:12px;color:#f9e2af;box-shadow:0 4px 16px #000000aa;max-width:600px;text-align:center}
#path-info .path-close{cursor:pointer;margin-left:10px;color:#6c7086;font-size:14px}
#path-info .path-close:hover{color:#cdd6f4}
#path-info .path-steps{color:#cdd6f4;margin-top:4px;font-size:11px}

/* --- Loading overlay --- */
#loading{position:fixed;top:0;left:0;width:100%;height:100%;background:#1e1e2e;display:flex;align-items:center;justify-content:center;z-index:999;font-size:14px;color:#6c7086}
#loading.hidden{display:none}
</style>
</head>
<body>
<div id="loading">Laying out graph&hellip;</div>
<div id="app">
<div id="topbar">
 <h1>__TITLE__</h1>
 <select id="layout-sel" title="Layout algorithm">
  <option value="cose">Force-directed</option>
  <option value="circle">Circle</option>
  <option value="concentric">Concentric</option>
  <option value="breadthfirst">Hierarchical</option>
  <option value="grid">Grid</option>
 </select>
 <span class="tb-sep"></span>
 <button id="fit-btn" title="Fit graph to viewport (F)">Fit</button>
 <button id="reset-btn" title="Clear selection and filters (Esc)">Reset</button>
 <button id="ghost-btn" title="Toggle: grey-out vs hide filtered elements (G)" class="active">Ghost</button>
 <button id="labels-btn" title="Toggle edge labels (L)">Edge Labels</button>
 <span class="tb-sep"></span>
 <button id="path-btn" title="Find shortest path: click start node, then end node (P)">Path</button>
 <button id="png-btn" title="Export as PNG">PNG</button>
 <span class="stats" id="stats"></span>
</div>

<div id="sidebar">
 <input type="text" id="search" placeholder="Search entities&hellip; (/)">
 <div class="filter-header"><h3>Entity Types</h3><span class="filter-toggle" data-target="type-filters">all</span><span class="filter-toggle" data-target="type-filters" data-mode="none">none</span></div>
 <div id="type-filters"></div>
 <div class="filter-header"><h3>Source Tools</h3><span class="filter-toggle" data-target="tool-filters">all</span><span class="filter-toggle" data-target="tool-filters" data-mode="none">none</span></div>
 <div id="tool-filters"></div>
 <div class="filter-header"><h3>Relationships</h3><span class="filter-toggle" data-target="rel-filters">all</span><span class="filter-toggle" data-target="rel-filters" data-mode="none">none</span></div>
 <div id="rel-filters"></div>
</div>

<div id="cy"></div>

<div id="detail">
 <div id="detail-header"><h2 id="detail-label"></h2><button id="close-detail">&times;</button></div>
 <div id="detail-id"></div>
 <div id="detail-badges"></div>
 <h3>Properties</h3>
 <table id="detail-props"></table>
 <h3>Connections</h3>
 <div id="detail-conns"></div>
 <h3>Sources</h3>
 <div id="detail-tools"></div>
</div>
</div>

<div id="tip"></div>
<div id="ctx-menu"></div>
<div id="path-info"><span id="path-text"></span><span class="path-close" id="path-close">&times;</span><div class="path-steps" id="path-steps"></div></div>

<script>
/* ── Data injected by Python ── */
var DATA = __GRAPH_DATA__;
var ENTITY_TYPES = __ENTITY_TYPES__;
var REL_TYPES = __REL_TYPES__;
var SOURCE_TOOLS = __SOURCE_TOOLS__;

/* ── Visual config ── */
var TYPE_COLORS = {
  person:'#89b4fa', organization:'#f38ba8', account:'#a6e3a1',
  document:'#9399b2', domain:'#cba6f7', email:'#fab387',
  username:'#94e2d5', phone:'#f9e2af', address:'#eba0ac',
  property:'#f2cdcd', vehicle:'#74c7ec',
};
var TYPE_SHAPES = {
  person:'ellipse', organization:'round-rectangle', account:'ellipse',
  document:'rectangle', domain:'diamond', email:'ellipse',
  username:'ellipse', phone:'ellipse', address:'round-rectangle',
  property:'round-rectangle', vehicle:'round-rectangle',
};
var REL_COLORS = {
  has_email:'#585b70', has_phone:'#585b70', has_username:'#585b70',
  has_account:'#585b70', has_address:'#585b70',
  also_known_as:'#89b4fa',
  works_at:'#f38ba8', officer_of:'#f38ba8', owns:'#f38ba8',
  controls:'#f38ba8', affiliated_with:'#f38ba8',
  donated_to:'#fab387', transacted_with:'#fab387',
  party_to:'#eba0ac', filed:'#eba0ac',
  follows:'#a6e3a1', connected_to:'#a6e3a1', mentioned:'#585b70',
  preceded_by:'#9399b2',
};

/* ── State ── */
var ghostMode = true;
var edgeLabelsOn = false;
var hiddenNodes = new Set();

/* ── Cytoscape styles ── */
var cyStyle = [
  {selector:'node', style:{
    'label':'data(label)', 'font-size':'10px', 'color':'#cdd6f4',
    'text-outline-color':'#1e1e2e', 'text-outline-width':2,
    'text-valign':'bottom', 'text-margin-y':4,
    'min-zoomed-font-size':8,
    'width':20, 'height':20,
    'border-width':1, 'border-color':'#45475a',
    'background-color':'#585b70',
    'transition-property':'opacity, border-color, border-width',
    'transition-duration':'0.15s',
  }},
  {selector:'node.filtered-out', style:{
    'opacity':0.06, 'label':'',
  }},
  {selector:'edge.filtered-out', style:{
    'opacity':0.03,
  }},
  {selector:'node.hidden', style:{
    'display':'none',
  }},
  {selector:'edge.hidden', style:{
    'display':'none',
  }},
  ...Object.entries(TYPE_COLORS).map(function(e){return {
    selector:'node[type="'+e[0]+'"]',
    style:{'background-color':e[1], 'shape':TYPE_SHAPES[e[0]]||'ellipse'},
  };}),
  {selector:'edge', style:{
    'width':1, 'line-color':'#45475a',
    'target-arrow-color':'#45475a', 'target-arrow-shape':'triangle',
    'arrow-scale':.5, 'curve-style':'bezier', 'opacity':.6,
    'label':'', 'font-size':'8px', 'color':'#6c7086',
    'text-outline-color':'#1e1e2e', 'text-outline-width':1.5,
    'text-rotation':'autorotate',
    'transition-property':'opacity, width',
    'transition-duration':'0.15s',
  }},
  ...Object.entries(REL_COLORS).map(function(e){return {
    selector:'edge[type="'+e[0]+'"]',
    style:{'line-color':e[1], 'target-arrow-color':e[1]},
  };}),
  {selector:'edge.show-label', style:{
    'label':'data(type)', 'min-zoomed-font-size':8,
  }},
  {selector:'node:selected', style:{'border-width':3, 'border-color':'#f5c2e7'}},
  {selector:'.dimmed', style:{'opacity':.08}},
  {selector:'.highlight', style:{'opacity':1}},
  {selector:'.neighbor', style:{'opacity':.85}},
  {selector:'edge.highlight', style:{'opacity':1, 'width':2.5}},
  {selector:'edge.highlight.show-label', style:{'label':'data(type)'}},
  {selector:'node.path-node', style:{'border-width':3, 'border-color':'#f9e2af', 'opacity':1}},
  {selector:'node.path-start', style:{'border-width':4, 'border-color':'#a6e3a1', 'opacity':1}},
  {selector:'node.path-end', style:{'border-width':4, 'border-color':'#f38ba8', 'opacity':1}},
  {selector:'edge.path-edge', style:{'line-color':'#f9e2af', 'target-arrow-color':'#f9e2af', 'width':3, 'opacity':1, 'z-index':10}},
];

/* ── Initialize Cytoscape ── */
var cy = cytoscape({
  container: document.getElementById('cy'),
  elements: DATA,
  style: cyStyle,
  layout: {name:'cose', animate:false, nodeRepulsion:function(){return 8192;},
           idealEdgeLength:function(){return 100;}, gravity:.25, numIter:1000,
           componentSpacing:80, randomize:true},
  wheelSensitivity: .3,
  minZoom: .05, maxZoom: 5,
});

/* Size nodes by degree */
cy.nodes().forEach(function(n){
  var d = n.degree();
  var s = Math.max(16, Math.min(56, 14 + d * 2.5));
  n.style({'width':s, 'height':s});
});

document.getElementById('loading').classList.add('hidden');
updateStats();

/* ── Stats ── */
function updateStats(){
  var total = cy.nodes().length;
  var totalE = cy.edges().length;
  var filt = cy.nodes('.filtered-out').length + cy.nodes('.hidden').length;
  var filtE = cy.edges('.filtered-out').length + cy.edges('.hidden').length;
  var el = document.getElementById('stats');
  if(filt === 0) el.textContent = total+' nodes, '+totalE+' edges';
  else el.textContent = (total-filt)+'/'+total+' nodes, '+(totalE-filtE)+'/'+totalE+' edges';
}

/* ── Build filter checkboxes ── */
function buildFilters(containerId, items, colorMap, isEdge){
  var el = document.getElementById(containerId);
  el.innerHTML = '';
  items.forEach(function(item){
    var color = colorMap ? (colorMap[item]||'#585b70') : '#585b70';
    var count = isEdge ? cy.edges('[type="'+item+'"]').length
                       : (containerId === 'type-filters'
                         ? cy.nodes('[type="'+item+'"]').length
                         : cy.nodes().filter(function(n){return n.data('tools').indexOf(item)>=0;}).length);
    var label = document.createElement('label');
    label.className = 'filter-item' + (isEdge ? ' edge-filter' : '');
    label.innerHTML = '<input type="checkbox" checked data-val="'+item+'">'
      + '<span class="dot" style="background:'+color+'"></span>'
      + '<span>'+item+'</span>'
      + '<span class="count">'+count+'</span>';
    el.appendChild(label);
  });
}
buildFilters('type-filters', ENTITY_TYPES, TYPE_COLORS, false);
buildFilters('tool-filters', SOURCE_TOOLS, null, false);
buildFilters('rel-filters', REL_TYPES, REL_COLORS, true);

/* ── All/None toggles ── */
document.querySelectorAll('.filter-toggle').forEach(function(btn){
  btn.addEventListener('click', function(){
    var target = this.dataset.target;
    var none = this.dataset.mode === 'none';
    document.querySelectorAll('#'+target+' input[type="checkbox"]').forEach(function(cb){ cb.checked = !none; });
    applyFilters();
  });
});

/* ── Filter logic — ghost mode vs hide mode ── */
function applyFilters(){
  var activeTypes = new Set(), activeTools = new Set(), activeRels = new Set();
  document.querySelectorAll('#type-filters input:checked').forEach(function(cb){activeTypes.add(cb.dataset.val);});
  document.querySelectorAll('#tool-filters input:checked').forEach(function(cb){activeTools.add(cb.dataset.val);});
  document.querySelectorAll('#rel-filters input:checked').forEach(function(cb){activeRels.add(cb.dataset.val);});
  var q = document.getElementById('search').value.toLowerCase().trim();

  cy.batch(function(){
    cy.nodes().forEach(function(n){
      var d = n.data();
      var typeOk = activeTypes.has(d.type);
      var toolOk = d.tools.some(function(t){return activeTools.has(t);});
      var searchOk = !q || d.fullLabel.toLowerCase().indexOf(q)>=0 || d.id.toLowerCase().indexOf(q)>=0;
      var manualHide = hiddenNodes.has(d.id);
      n.removeClass('filtered-out hidden');
      if(manualHide){ n.addClass('hidden'); }
      else if(!(typeOk && toolOk && searchOk)){
        n.addClass(ghostMode ? 'filtered-out' : 'hidden');
      }
    });
    cy.edges().forEach(function(e){
      var relOk = activeRels.has(e.data('type'));
      var srcOk = !e.source().hasClass('filtered-out') && !e.source().hasClass('hidden');
      var tgtOk = !e.target().hasClass('filtered-out') && !e.target().hasClass('hidden');
      e.removeClass('filtered-out hidden');
      if(!relOk || !srcOk || !tgtOk){
        e.addClass(ghostMode ? 'filtered-out' : 'hidden');
      }
    });
  });
  updateStats();
}

document.getElementById('sidebar').addEventListener('change', applyFilters);
var searchTimer;
document.getElementById('search').addEventListener('input', function(){
  clearTimeout(searchTimer);
  searchTimer = setTimeout(applyFilters, 200);
});

/* ── Ghost toggle ── */
document.getElementById('ghost-btn').addEventListener('click', function(){
  ghostMode = !ghostMode;
  this.classList.toggle('active', ghostMode);
  applyFilters();
});

/* ── Edge labels toggle ── */
document.getElementById('labels-btn').addEventListener('click', function(){
  edgeLabelsOn = !edgeLabelsOn;
  this.classList.toggle('active', edgeLabelsOn);
  cy.edges()[edgeLabelsOn ? 'addClass' : 'removeClass']('show-label');
});

/* ── Node click → highlight neighbors + detail panel ── */
cy.on('tap', 'node', function(evt){
  if(pathMode) return;  /* Path mode has its own tap handler */
  var node = evt.target;
  if(node.hasClass('filtered-out') || node.hasClass('hidden')) return;
  var neighborhood = node.neighborhood().add(node);
  cy.elements().removeClass('highlight neighbor dimmed');
  cy.elements().not('.filtered-out').not('.hidden').addClass('dimmed');
  neighborhood.removeClass('dimmed');
  node.addClass('highlight');
  neighborhood.nodes().not(node).addClass('neighbor');
  neighborhood.edges().addClass('highlight');
  showDetail(node);
});

cy.on('tap', function(evt){
  if(evt.target === cy){
    cy.elements().removeClass('highlight neighbor dimmed');
    document.getElementById('app').classList.remove('detail-open');
    closeCtxMenu();
  }
});

/* ── Rich tooltip ── */
var tipEl = document.getElementById('tip');
function showTip(x, y, html){
  tipEl.innerHTML = html;
  tipEl.style.display = 'block';
  tipEl.style.left = (x+14)+'px';
  tipEl.style.top = (y+14)+'px';
  /* Keep on screen */
  var r = tipEl.getBoundingClientRect();
  if(r.right > window.innerWidth) tipEl.style.left = (x - r.width - 8)+'px';
  if(r.bottom > window.innerHeight) tipEl.style.top = (y - r.height - 8)+'px';
}
function hideTip(){ tipEl.style.display = 'none'; }

cy.on('mouseover', 'node', function(evt){
  var d = evt.target.data();
  if(evt.target.hasClass('filtered-out')) return;
  var color = TYPE_COLORS[d.type] || '#585b70';
  var h = '<div class="tip-label">'+escH(d.fullLabel)+'</div>';
  h += '<span class="tip-badge" style="background:'+color+'22;color:'+color+';border:1px solid '+color+'44">'+d.type+'</span>';
  h += '<div class="tip-row">Connections: <span>'+evt.target.degree()+'</span></div>';
  /* Show first useful prop */
  if(d.props){
    var keys = Object.keys(d.props);
    for(var i=0;i<keys.length&&i<2;i++){
      var v = d.props[keys[i]];
      var vs = typeof v === 'object' ? JSON.stringify(v) : String(v);
      if(vs.length > 60) vs = vs.substring(0,57)+'...';
      h += '<div class="tip-row">'+escH(keys[i])+': <span>'+escH(vs)+'</span></div>';
    }
  }
  if(d.tools.length) h += '<div class="tip-row">Sources: <span>'+d.tools.join(', ')+'</span></div>';
  h += '<div class="tip-id">'+escH(d.id)+'</div>';
  var pos = evt.originalEvent || evt.renderedPosition;
  showTip(pos.clientX || pos.x, pos.clientY || pos.y, h);
});
cy.on('mouseover', 'edge', function(evt){
  if(evt.target.hasClass('filtered-out')) return;
  var d = evt.target.data();
  var color = REL_COLORS[d.type] || '#585b70';
  var h = '<div class="tip-label" style="color:'+color+'">'+d.type+'</div>';
  h += '<div class="tip-row">'+escH(d.source)+' &rarr; '+escH(d.target)+'</div>';
  if(d.props){
    Object.keys(d.props).forEach(function(k){
      var v = d.props[k];
      var vs = typeof v === 'object' ? JSON.stringify(v) : String(v);
      if(vs.length > 60) vs = vs.substring(0,57)+'...';
      h += '<div class="tip-row">'+escH(k)+': <span>'+escH(vs)+'</span></div>';
    });
  }
  var pos = evt.originalEvent || evt.renderedPosition;
  showTip(pos.clientX || pos.x, pos.clientY || pos.y, h);
});
cy.on('mouseout', 'node,edge', hideTip);
cy.on('mousemove', function(evt){
  if(tipEl.style.display==='block' && evt.originalEvent){
    tipEl.style.left = (evt.originalEvent.clientX+14)+'px';
    tipEl.style.top = (evt.originalEvent.clientY+14)+'px';
  }
});

function escH(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

/* ── Context menu ── */
var ctxMenu = document.getElementById('ctx-menu');
var ctxNode = null;

cy.on('cxttap', 'node', function(evt){
  var oe = evt.originalEvent || {};
  if(oe.preventDefault) oe.preventDefault();
  ctxNode = evt.target;
  var rp = evt.renderedPosition || ctxNode.renderedPosition();
  var cr = document.getElementById('cy').getBoundingClientRect();
  var x = oe.clientX || (cr.left + rp.x);
  var y = oe.clientY || (cr.top + rp.y);
  var d = ctxNode.data();
  var items = [
    {label:'Expand neighborhood', key:'E', action:'expand'},
    {label:'Focus component', key:'C', action:'component'},
    {sep:true},
    {label:'Hide this node', key:'H', action:'hide'},
    {label:'Hide unconnected', action:'hide-orphan'},
    {sep:true},
    {label:'Copy entity ID', action:'copy-id'},
  ];
  if(d.props && d.props.url) items.push({label:'Open URL', action:'open-url'});
  var html = '';
  items.forEach(function(it){
    if(it.sep){ html += '<div class="ctx-sep"></div>'; return; }
    html += '<div class="ctx-item" data-action="'+it.action+'">';
    html += it.label;
    if(it.key) html += '<span class="ctx-key">'+it.key+'</span>';
    html += '</div>';
  });
  ctxMenu.innerHTML = html;
  ctxMenu.style.display = 'block';
  ctxMenu.style.left = x+'px';
  ctxMenu.style.top = y+'px';
  /* Keep on screen */
  var r = ctxMenu.getBoundingClientRect();
  if(r.right > window.innerWidth) ctxMenu.style.left = (x - r.width)+'px';
  if(r.bottom > window.innerHeight) ctxMenu.style.top = (y - r.height)+'px';
});

document.addEventListener('click', function(e){
  if(!ctxMenu.contains(e.target)) closeCtxMenu();
});

ctxMenu.addEventListener('click', function(e){
  var item = e.target.closest('.ctx-item');
  if(!item || !ctxNode) return;
  var action = item.dataset.action;

  if(action === 'expand'){
    var hood = ctxNode.neighborhood().add(ctxNode);
    cy.elements().removeClass('highlight neighbor dimmed');
    cy.elements().not('.filtered-out').not('.hidden').addClass('dimmed');
    hood.removeClass('dimmed');
    ctxNode.addClass('highlight');
    hood.nodes().not(ctxNode).addClass('neighbor');
    hood.edges().addClass('highlight');
    showDetail(ctxNode);
  }
  else if(action === 'component'){
    /* BFS to find connected component */
    var visited = new Set();
    var queue = [ctxNode.id()];
    while(queue.length){
      var nid = queue.shift();
      if(visited.has(nid)) continue;
      visited.add(nid);
      cy.getElementById(nid).neighborhood().nodes().forEach(function(nb){
        if(!visited.has(nb.id())) queue.push(nb.id());
      });
    }
    cy.elements().removeClass('highlight neighbor dimmed');
    cy.elements().not('.filtered-out').not('.hidden').addClass('dimmed');
    visited.forEach(function(vid){
      var n = cy.getElementById(vid);
      n.removeClass('dimmed').addClass('neighbor');
      n.connectedEdges().forEach(function(e){
        var oid = e.source().id() === vid ? e.target().id() : e.source().id();
        if(visited.has(oid)) e.removeClass('dimmed').addClass('highlight');
      });
    });
    ctxNode.removeClass('neighbor').addClass('highlight');
    cy.fit(cy.nodes().filter(function(n){return visited.has(n.id());}), 40);
  }
  else if(action === 'hide'){
    hiddenNodes.add(ctxNode.id());
    applyFilters();
    document.getElementById('app').classList.remove('detail-open');
    cy.elements().removeClass('highlight neighbor dimmed');
  }
  else if(action === 'hide-orphan'){
    cy.nodes().forEach(function(n){
      if(n.degree() === 0) hiddenNodes.add(n.id());
    });
    applyFilters();
  }
  else if(action === 'copy-id'){
    navigator.clipboard.writeText(ctxNode.id());
  }
  else if(action === 'open-url'){
    var url = ctxNode.data().props.url;
    if(url) window.open(url, '_blank', 'noopener');
  }
  closeCtxMenu();
});

function closeCtxMenu(){
  ctxMenu.style.display = 'none';
  ctxNode = null;
}

/* ── Detail panel ── */
function showDetail(node){
  var d = node.data();
  var color = TYPE_COLORS[d.type] || '#585b70';
  document.getElementById('detail-label').textContent = d.fullLabel;
  document.getElementById('detail-id').textContent = d.id;
  document.getElementById('detail-badges').innerHTML =
    '<span class="badge" style="background:'+color+'22;color:'+color+';border:1px solid '+color+'44">'+d.type+'</span>'
    + '<span class="degree-badge">'+node.degree()+' connections</span>';

  /* Properties */
  var tbody = '';
  if(d.props){
    Object.keys(d.props).sort().forEach(function(k){
      var v = d.props[k];
      var vs;
      if(Array.isArray(v)){
        vs = v.map(function(x){ return escH(String(x)); }).join(', ');
      } else if(typeof v === 'object' && v !== null){
        vs = '<span style="color:#585b70">'+escH(JSON.stringify(v))+'</span>';
      } else {
        vs = String(v);
      }
      /* Linkify URLs */
      if(typeof vs === 'string' && vs.match(/^https?:\/\//)){
        var display = vs.length > 50 ? vs.substring(0,47)+'...' : vs;
        vs = '<a href="'+escH(vs)+'" target="_blank" rel="noopener" title="'+escH(vs)+'">'+escH(display)+'</a>';
      }
      tbody += '<tr><th>'+escH(k)+'</th><td>'+vs+'</td></tr>';
    });
  }
  document.getElementById('detail-props').innerHTML = tbody || '<tr><td colspan="2" style="color:#585b70">None</td></tr>';

  /* Connections — grouped by relationship type */
  var connsByType = {};
  node.connectedEdges().forEach(function(e){
    if(e.hasClass('hidden')) return;
    var other = e.source().id() === node.id() ? e.target() : e.source();
    if(other.hasClass('hidden')) return;
    var rel = e.data('type');
    var dir = e.source().id() === node.id() ? '\u2192' : '\u2190';
    if(!connsByType[rel]) connsByType[rel] = [];
    connsByType[rel].push({dir:dir, other:other});
  });

  var conns = '';
  Object.keys(connsByType).sort().forEach(function(rel){
    var relColor = REL_COLORS[rel] || '#585b70';
    conns += '<div class="conn-group-header"><span class="dot" style="background:'+relColor+'"></span>'+rel+' ('+connsByType[rel].length+')</div>';
    connsByType[rel].forEach(function(c){
      var otherColor = TYPE_COLORS[c.other.data('type')] || '#585b70';
      var dimClass = c.other.hasClass('filtered-out') ? ' style="opacity:.4"' : '';
      conns += '<div class="conn-item" data-id="'+c.other.id()+'"'+dimClass+'>'
        + '<span class="conn-dir">'+c.dir+'</span>'
        + '<span class="conn-dot" style="background:'+otherColor+'"></span>'
        + '<span class="conn-label">'+escH(c.other.data('fullLabel'))+'</span>'
        + '<span class="conn-type-tag">'+c.other.data('type')+'</span>'
        + '</div>';
    });
  });
  document.getElementById('detail-conns').innerHTML = conns || '<span style="color:#585b70">None</span>';

  /* Make connections clickable */
  document.querySelectorAll('.conn-item').forEach(function(el){
    el.addEventListener('click', function(){
      var tid = this.dataset.id;
      var tn = cy.getElementById(tid);
      if(tn.length){
        cy.animate({center:{eles:tn}, zoom:Math.max(cy.zoom(), 1.5)}, {duration:300});
        setTimeout(function(){ tn.emit('tap'); }, 350);
      }
    });
  });

  /* Sources */
  var tools = '';
  d.tools.forEach(function(t){ tools += '<span class="tool-tag">'+t+'</span>'; });
  document.getElementById('detail-tools').innerHTML = tools || '<span style="color:#585b70">Unknown</span>';

  document.getElementById('app').classList.add('detail-open');
}

document.getElementById('close-detail').addEventListener('click', function(){
  document.getElementById('app').classList.remove('detail-open');
  cy.elements().removeClass('highlight neighbor dimmed');
});

/* ── Layout selector ── */
document.getElementById('layout-sel').addEventListener('change', function(){
  var name = this.value;
  var opts = {name:name, animate:true, animationDuration:500};
  if(name==='cose'){
    opts.animate = false;
    opts.nodeRepulsion = function(){return 8192;};
    opts.idealEdgeLength = function(){return 100;};
    opts.gravity = .25;
    opts.numIter = 1000;
  }
  if(name==='concentric'){
    opts.concentric = function(n){return n.degree();};
    opts.levelWidth = function(){return 2;};
  }
  if(name==='breadthfirst'){
    opts.directed = true;
    opts.spacingFactor = 1.2;
  }
  document.getElementById('loading').classList.remove('hidden');
  setTimeout(function(){
    cy.layout(opts).run();
    setTimeout(function(){document.getElementById('loading').classList.add('hidden');}, 100);
  }, 50);
});

/* ── Toolbar buttons ── */
document.getElementById('fit-btn').addEventListener('click', function(){cy.fit(null, 30);});

document.getElementById('reset-btn').addEventListener('click', function(){
  document.getElementById('search').value = '';
  document.querySelectorAll('#sidebar input[type="checkbox"]').forEach(function(cb){cb.checked=true;});
  cy.elements().removeClass('highlight neighbor dimmed');
  hiddenNodes.clear();
  applyFilters();
  cy.fit(null, 30);
  document.getElementById('app').classList.remove('detail-open');
});

document.getElementById('png-btn').addEventListener('click', function(){
  var png = cy.png({full:true, bg:'#1e1e2e', scale:2});
  var a = document.createElement('a');
  a.href = png; a.download = 'graph.png'; a.click();
});

/* ── Path-finding mode ── */
var pathMode = false;
var pathStart = null;
var pathBtn = document.getElementById('path-btn');
var pathInfo = document.getElementById('path-info');
var pathText = document.getElementById('path-text');
var pathSteps = document.getElementById('path-steps');

function togglePathMode(){
  pathMode = !pathMode;
  pathBtn.classList.toggle('active', pathMode);
  if(pathMode){
    pathText.textContent = 'Click a start node\u2026';
    pathSteps.innerHTML = '';
    pathInfo.style.display = 'block';
    pathStart = null;
    clearPath();
  } else {
    clearPath();
    pathInfo.style.display = 'none';
    pathStart = null;
  }
}

function clearPath(){
  cy.elements().removeClass('path-node path-start path-end path-edge dimmed');
}

function bfsShortestPath(startId, endId){
  /* BFS returning array of node IDs from start to end, or null. */
  var visited = {};
  var queue = [startId];
  visited[startId] = null;  /* parent pointer */
  while(queue.length){
    var cur = queue.shift();
    if(cur === endId){
      /* Reconstruct path */
      var path = [];
      var n = endId;
      while(n !== null){ path.unshift(n); n = visited[n]; }
      return path;
    }
    var node = cy.getElementById(cur);
    node.neighborhood().nodes().forEach(function(nb){
      var nid = nb.id();
      if(!(nid in visited) && !nb.hasClass('hidden')){
        visited[nid] = cur;
        queue.push(nid);
      }
    });
  }
  return null;
}

function showPath(nodeIds){
  /* Dim everything, then highlight path nodes and edges. */
  cy.batch(function(){
    cy.elements().not('.filtered-out').not('.hidden').addClass('dimmed');
    for(var i = 0; i < nodeIds.length; i++){
      var n = cy.getElementById(nodeIds[i]);
      n.removeClass('dimmed').addClass('path-node');
      if(i === 0) n.addClass('path-start');
      if(i === nodeIds.length - 1) n.addClass('path-end');
      /* Highlight edge to next node in path */
      if(i < nodeIds.length - 1){
        var nextId = nodeIds[i + 1];
        n.connectedEdges().forEach(function(e){
          var oid = e.source().id() === nodeIds[i] ? e.target().id() : e.source().id();
          if(oid === nextId){ e.removeClass('dimmed').addClass('path-edge'); }
        });
      }
    }
  });
  /* Build step description */
  var hops = nodeIds.length - 1;
  pathText.textContent = hops + ' hop' + (hops !== 1 ? 's' : '') + ' between nodes';
  var stepsHtml = '';
  for(var j = 0; j < nodeIds.length; j++){
    var nd = cy.getElementById(nodeIds[j]).data();
    var color = TYPE_COLORS[nd.type] || '#585b70';
    stepsHtml += '<span style="color:'+color+'">'+escH(nd.fullLabel)+'</span>';
    if(j < nodeIds.length - 1){
      /* Find the relationship type for this hop */
      var fromNode = cy.getElementById(nodeIds[j]);
      var relType = '';
      fromNode.connectedEdges().forEach(function(e){
        var oid = e.source().id() === nodeIds[j] ? e.target().id() : e.source().id();
        if(oid === nodeIds[j+1]) relType = e.data('type');
      });
      stepsHtml += ' <span style="color:#6c7086">\u2192 '+escH(relType)+' \u2192</span> ';
    }
  }
  pathSteps.innerHTML = stepsHtml;
  pathInfo.style.display = 'block';
  /* Fit the path into view */
  var pathEles = cy.collection();
  nodeIds.forEach(function(nid){ pathEles = pathEles.add(cy.getElementById(nid)); });
  cy.fit(pathEles, 60);
}

pathBtn.addEventListener('click', togglePathMode);

document.getElementById('path-close').addEventListener('click', function(){
  pathMode = false;
  pathBtn.classList.remove('active');
  clearPath();
  pathInfo.style.display = 'none';
  pathStart = null;
});

/* Intercept node taps in path mode */
cy.on('tap', 'node', function(evt){
  if(!pathMode) return;
  var node = evt.target;
  if(node.hasClass('filtered-out') || node.hasClass('hidden')) return;
  evt.stopPropagation();
  if(!pathStart){
    pathStart = node.id();
    clearPath();
    node.addClass('path-start path-node');
    pathText.textContent = 'Start: ' + node.data('fullLabel') + ' \u2014 click an end node\u2026';
    pathSteps.innerHTML = '';
  } else {
    var endId = node.id();
    if(endId === pathStart){
      pathText.textContent = 'Start and end are the same node. Click a different end node\u2026';
      return;
    }
    var path = bfsShortestPath(pathStart, endId);
    if(path){
      showPath(path);
    } else {
      clearPath();
      pathText.textContent = 'No path found between these nodes.';
      pathSteps.innerHTML = '';
      cy.getElementById(pathStart).addClass('path-start path-node');
      node.addClass('path-end path-node');
    }
    pathStart = null;
    pathMode = false;
    pathBtn.classList.remove('active');
  }
});

/* ── Keyboard shortcuts ── */
document.addEventListener('keydown', function(e){
  if(e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if(e.key === 'Escape'){
    cy.elements().removeClass('highlight neighbor dimmed');
    document.getElementById('app').classList.remove('detail-open');
    closeCtxMenu();
    hideTip();
    if(pathMode){ pathMode = false; pathBtn.classList.remove('active'); clearPath(); pathInfo.style.display = 'none'; pathStart = null; }
  }
  else if(e.key === 'p' || e.key === 'P'){ togglePathMode(); }
  else if(e.key === 'f' || e.key === 'F'){ cy.fit(null, 30); }
  else if(e.key === 'g' || e.key === 'G'){ document.getElementById('ghost-btn').click(); }
  else if(e.key === 'l' || e.key === 'L'){ document.getElementById('labels-btn').click(); }
  else if(e.key === '/'){
    e.preventDefault();
    document.getElementById('search').focus();
  }
});
</script>
</body>
</html>"""
