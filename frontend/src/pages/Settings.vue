<script setup>
import { onMounted, ref } from 'vue'
import { getJSON, putJSON } from '../api'

const LABEL = {
  default_sample_rate: '默认采样率（旧平采遗留）',
  require_checklist_pass: '放行须清单全过',
  sample_mode: '采样模式',
}

function displayValue(k, v) {
  if (k === 'require_checklist_pass') {
    return v === '1' || v === 1 || v === true || v === 'true' ? '是' : '否'
  }
  if (k === 'sample_mode') {
    return ({ stratified: '分层配额抽样', deterministic: '确定性抽样', seeded: '确定性抽样' })[v] || v
  }
  return v
}

const settings = ref({})
const quotas = ref([])
const forms = ref({})
const results = ref({})
const err = ref('')

function buildForm(q) {
  const c = q.current || {}
  const quotasMap = {}
  for (const s of q.strata) quotasMap[s.stratum] = (c.quotas || {})[s.stratum] ?? 1
  return {
    quota_mode: c.quota_mode || 'count',
    quotas: quotasMap,
    shortfall_policy: c.shortfall_policy || 'best_effort',
    stale_policy: c.stale_policy || 'freeze',
  }
}

async function refresh() {
  settings.value = await getJSON('/api/settings')
  quotas.value = await getJSON('/api/quota')
  const f = {}
  for (const q of quotas.value) f[q.subject_id] = buildForm(q)
  forms.value = f
}

onMounted(async () => {
  try { await refresh() } catch (e) { err.value = String(e.message || e) }
})

async function save(q) {
  err.value = ''
  results.value = { ...results.value, [q.subject_id]: null }
  const f = forms.value[q.subject_id]
  const quotasMap = {}
  for (const s of q.strata) quotasMap[s.stratum] = Number(f.quotas[s.stratum])
  try {
    const r = await putJSON(`/api/quota/${q.subject_id}`, {
      quota_mode: f.quota_mode,
      quotas: quotasMap,
      shortfall_policy: f.shortfall_policy,
      stale_policy: f.stale_policy,
    })
    results.value = { ...results.value, [q.subject_id]: r.detail }
    quotas.value = await getJSON('/api/quota')
    const nf = {}
    for (const x of quotas.value) nf[x.subject_id] = buildForm(x)
    forms.value = nf
  } catch (e) {
    err.value = String(e.message || e)
  }
}
</script>
<template>
  <div class="page">
    <h1>设置</h1>
    <p class="muted">分层配额按对象版本化；不足配额与旧配额处置均为二选一单值策略，同一时刻只生效一种。</p>
    <p v-if="err" class="tag danger">{{ err }}</p>

    <div class="panel" v-for="q in quotas" :key="q.subject_id">
      <h3>
        {{ q.subject_title }}
        <span class="tag" v-if="q.current">当前 v{{ q.current.version }}</span>
      </h3>
      <p class="muted">
        分层字段 <span class="mono">{{ q.strata_field }}</span>（夹具约定）·
        层源条数：
        <span v-for="s in q.strata" :key="s.stratum" class="tag" style="margin-right:0.25rem">
          {{ s.stratum }} {{ s.source_count }} 条
        </span>
      </p>
      <div class="row" v-if="forms[q.subject_id]">
        <div class="field">
          <label>配额方式</label>
          <label><input type="radio" value="count" v-model="forms[q.subject_id].quota_mode" /> 每层目标条数</label>
          <label><input type="radio" value="rate" v-model="forms[q.subject_id].quota_mode" /> 层内采样率</label>
        </div>
        <div class="field" v-for="s in q.strata" :key="s.stratum">
          <label>{{ s.stratum }}（源 {{ s.source_count }}）</label>
          <input
            type="number"
            :min="forms[q.subject_id].quota_mode === 'count' ? 1 : 0.05"
            :max="forms[q.subject_id].quota_mode === 'count' ? s.source_count : 1"
            :step="forms[q.subject_id].quota_mode === 'count' ? 1 : 0.05"
            v-model.number="forms[q.subject_id].quotas[s.stratum]"
          />
        </div>
      </div>
      <div class="row" v-if="forms[q.subject_id]">
        <div class="field">
          <label>源不足配额时（二选一）</label>
          <label><input type="radio" value="strict" v-model="forms[q.subject_id].shortfall_policy" /> 严格：整次采样失败，不落批次</label>
          <label><input type="radio" value="best_effort" v-model="forms[q.subject_id].shortfall_policy" /> 尽力：能抽多少算多少，未满批次禁止放行</label>
        </div>
        <div class="field">
          <label>配额变更时旧配额未放行包（二选一）</label>
          <label><input type="radio" value="invalidate" v-model="forms[q.subject_id].stale_policy" /> 作废受影响未放行包</label>
          <label><input type="radio" value="freeze" v-model="forms[q.subject_id].stale_policy" /> 保留只读对照，禁止放行</label>
        </div>
        <button @click="save(q)">保存为新版本</button>
      </div>
      <p v-if="results[q.subject_id]" class="tag ok" style="margin-top:0.5rem">{{ results[q.subject_id] }}</p>
    </div>

    <div class="panel">
      <h3>底座策略（只读）</h3>
      <table>
        <thead>
          <tr>
            <th>配置项</th>
            <th>取值</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(v, k) in settings" :key="k">
            <td>{{ LABEL[k] || k }}</td>
            <td><span class="tag">{{ displayValue(k, v) }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
