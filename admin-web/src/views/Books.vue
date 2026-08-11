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
      <el-table-column label="操作" width="230">
        <template #default="{ row }">
          <el-button size="small" @click="reviewAll(row)">批量机审</el-button>
          <el-button v-if="row.status !== 'on_shelf'" type="success" size="small"
                     :loading="acting" @click="shelf(row, true)">上架</el-button>
          <el-button v-else type="warning" size="small"
                     :loading="acting" @click="shelf(row, false)">下架</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dlg" title="新建书籍" width="520px">
      <el-alert type="info" :closable="false" style="margin-bottom:14px"
                title="流程：选题材 → 生成大纲 → 后台逐章生成（约每章1-2分钟）→ 批量机审 → 审核队列复核 → 上架" />
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
        <el-form-item label="立即生成"><el-switch v-model="form.auto_generate" /></el-form-item>
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
  form.value = { theme_id: themes.value[0]?.id, title: '', chapters: 30, auto_generate: true }
  dlg.value = true
}
async function create() {
  if (!form.value.title) { ElMessage.warning('请填书名'); return }
  creating.value = true
  try {
    await api.post('/books', {
      ...form.value,
      dedup_key: 'book-' + Date.now(),
    })
    ElMessage.success('已创建，后台生成中（可看状态列）')
    dlg.value = false; load()
  } finally { creating.value = false }
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
