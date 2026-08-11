<template>
  <el-card>
    <template #header>
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span style="font-weight:700">书籍管理</span>
        <div>
          <el-button size="small" @click="load">刷新</el-button>
          <el-button type="primary" size="small" @click="openCreate">新建书籍</el-button>
        </div>
      </div>
    </template>
    <el-table :data="rows" v-loading="loading">
      <el-table-column prop="id" label="ID" width="60" />
      <el-table-column prop="title" label="书名" width="140" />
      <el-table-column prop="theme" label="题材" width="110" />
      <el-table-column label="运营简介" min-width="280">
        <template #default="{ row }">
          <span style="font-size:13px;color:#555">{{ row.intro || '暂无' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="章节进度" width="110">
        <template #default="{ row }">{{ row.chapters }}/{{ row.total_chapters }}</template>
      </el-table-column>
      <el-table-column prop="status" label="状态" width="110">
        <template #default="{ row }">
          <el-tag :type="{ on_shelf: 'success', off_shelf: 'info', draft: 'warning' }[row.status] || 'warning'">
            {{ { on_shelf: '已上架', off_shelf: '已下架', draft: '草稿', generating: '生成中', reviewing: '审核中' }[row.status] || row.status }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="300">
        <template #default="{ row }">
          <el-button v-if="row.status === 'draft'" type="primary" size="small"
                     @click="openOutline(row)">编辑大纲</el-button>
          <el-button size="small" @click="reviewAll(row)">批量机审</el-button>
          <el-button v-if="row.status !== 'on_shelf' && row.status !== 'draft'" type="success" size="small"
                     :loading="acting" @click="shelf(row, true)">上架</el-button>
          <el-button v-if="row.status === 'on_shelf'" type="warning" size="small"
                     :loading="acting" @click="shelf(row, false)">下架</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="outlineDlg" title="大纲与简介（人工干预）" width="720px" top="4vh">
      <el-alert type="warning" :closable="false" style="margin-bottom:12px"
                title="逐章生成以大纲为准：改章节走向/角色表后再点开始生成。确认后不可再改。" />
      <el-form label-width="90px">
        <el-form-item label="书名">
          <el-input v-model="outlineForm.title" disabled />
        </el-form-item>
        <el-form-item label="运营简介">
          <el-input v-model="outlineForm.intro" type="textarea" :rows="3" maxlength="300" show-word-limit />
        </el-form-item>
        <el-form-item label="章数">
          <el-input-number v-model="outlineForm.total_chapters" :min="5" :max="100" />
        </el-form-item>
        <el-form-item label="全书大纲">
          <el-input v-model="outlineForm.outline" type="textarea" :rows="16"
                    style="font-family:monospace" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="outlineDlg = false">取消</el-button>
        <el-button :loading="savingOutline" @click="saveOutline">保存修改</el-button>
        <el-button type="primary" :loading="starting" @click="saveAndGenerate">
          保存并开始生成
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="dlg" title="新建书籍" width="520px">
      <el-alert type="info" :closable="false" style="margin-bottom:14px"
                title="流程：选题材建书 → 编辑大纲/简介（人工干预）→ 开始后台生成 → 批量机审 → 审核队列复核 → 上架" />
      <el-form label-width="90px">
        <el-form-item label="题材">
          <el-select v-model="form.theme_id" style="width:100%">
            <el-option v-for="t in themes" :key="t.id" :label="t.name" :value="t.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="书名"><el-input v-model="form.title" placeholder="自定义书名" /></el-form-item>
        <el-form-item label="章数">
          <el-input-number v-model="form.chapters" :min="5" :max="100" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlg = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="create">创建</el-button>
      </template>
    </el-dialog>
  </el-card>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../api'

const rows = ref([]), themes = ref([]), loading = ref(false), acting = ref(false)
const dlg = ref(false), creating = ref(false)
const form = ref({})
let timer = null

async function load() {
  loading.value = true
  try {
    rows.value = await api.get('/books')
    // 有书在生成中则 10s 后轮询
    const generating = rows.value.some(b => b.status === 'generating')
    clearTimeout(timer)
    if (generating) timer = setTimeout(load, 10000)
  } finally { loading.value = false }
}
async function loadThemes() { themes.value = await api.get('/themes') }

function openCreate() {
  form.value = { theme_id: themes.value[0]?.id, title: '', chapters: 30 }
  dlg.value = true
}
async function create() {
  if (!form.value.title) { ElMessage.warning('请填书名'); return }
  creating.value = true
  try {
    const d = await api.post('/books', {
      ...form.value, auto_generate: false,
      dedup_key: 'book-' + Date.now(),
    })
    ElMessage.success('大纲已生成，请先编辑大纲再开始生成')
    dlg.value = false
    await load()
    openOutline({ id: d.book_id, title: form.value.title })
  } finally { creating.value = false }
}

// 大纲人工干预
const outlineDlg = ref(false), savingOutline = ref(false), starting = ref(false)
const outlineForm = ref({})
async function openOutline(row) {
  const d = await api.get('/books/' + row.id)
  outlineForm.value = { id: d.id, title: d.title, intro: d.intro || '',
                        outline: d.outline || '', total_chapters: d.total_chapters }
  outlineDlg.value = true
}
async function saveOutline() {
  savingOutline.value = true
  try {
    await api.put(`/books/${outlineForm.value.id}/outline`, {
      outline: outlineForm.value.outline,
      intro: outlineForm.value.intro,
      total_chapters: outlineForm.value.total_chapters,
    })
    ElMessage.success('已保存')
  } finally { savingOutline.value = false }
}
async function saveAndGenerate() {
  await saveOutline()
  starting.value = true
  try {
    await api.post(`/books/${outlineForm.value.id}/generate`, {})
    ElMessage.success('已开始后台逐章生成，状态列会显示进度')
    outlineDlg.value = false; load()
  } finally { starting.value = false }
}
async function reviewAll(row) {
  const d = await api.post(`/books/${row.id}/machine-review-all`)
  ElMessage.success(`机审完成：通过 ${d.machine_pass}，命中 ${d.machine_hit}`)
}
async function shelf(row, on) {
  acting.value = true
  try {
    await api.post(`/books/${row.id}/${on ? 'shelf' : 'off-shelf'}`)
    ElMessage.success(on ? '已上架' : '已下架'); load()
  } finally { acting.value = false }
}
onMounted(() => { load(); loadThemes() })
onUnmounted(() => clearTimeout(timer))
</script>
