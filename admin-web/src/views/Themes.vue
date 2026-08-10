<template>
  <el-card>
    <template #header>
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span style="font-weight:700">题材模板</span>
        <el-button type="primary" size="small" @click="openEdit(null)">新建题材</el-button>
      </div>
    </template>
    <el-table :data="rows" v-loading="loading">
      <el-table-column prop="id" label="ID" width="60" />
      <el-table-column prop="name" label="名称" width="140" />
      <el-table-column prop="weight" label="权重" width="90" />
      <el-table-column prop="target_chapters" label="目标章数" width="100" />
      <el-table-column prop="enabled" label="状态" width="90">
        <template #default="{ row }">
          <el-tag :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '停用' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="120">
        <template #default="{ row }">
          <el-button size="small" @click="openEdit(row)">编辑</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dlg" :title="form.id ? '编辑题材' : '新建题材'" width="560px">
      <el-form label-width="90px">
        <el-form-item label="名称"><el-input v-model="form.name" /></el-form-item>
        <el-form-item label="权重"><el-input-number v-model="form.weight" :min="0" :max="100" /></el-form-item>
        <el-form-item label="目标章数"><el-input-number v-model="form.target_chapters" :min="1" :max="200" /></el-form-item>
        <el-form-item label="提示词">
          <el-input v-model="form.prompt_template" type="textarea" :rows="5" />
        </el-form-item>
        <el-form-item label="启用"><el-switch v-model="form.enabled" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlg = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </el-card>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../api'

const rows = ref([]), loading = ref(false), dlg = ref(false), saving = ref(false)
const form = ref({})

async function load() {
  loading.value = true
  try { rows.value = await api.get('/themes') } finally { loading.value = false }
}
function openEdit(row) {
  form.value = row ? { ...row } : { name: '', weight: 1, target_chapters: 30, prompt_template: '', enabled: true }
  dlg.value = true
}
async function save() {
  saving.value = true
  try {
    if (form.value.id) await api.put('/themes/' + form.value.id, form.value)
    else await api.post('/themes', form.value)
    ElMessage.success('已保存'); dlg.value = false; load()
  } finally { saving.value = false }
}
onMounted(load)
</script>
