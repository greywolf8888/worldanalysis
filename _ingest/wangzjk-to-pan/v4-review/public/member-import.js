const input = document.getElementById('member-links');
const save = document.getElementById('save-member-links');
const result = document.getElementById('member-result');

save.addEventListener('click', async () => {
  if (!input.value.trim()) {
    result.textContent = '请先填写项目链接清单。';
    return;
  }
  save.disabled = true;
  result.textContent = '正在核对项目…';
  try {
    const response = await fetch('/api/member-share-import', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: input.value
    });
    const data = await response.json();
    if (!response.ok) {
      result.textContent = data.error || '导入未完成，请重试。';
      return;
    }
    input.value = '';
    result.textContent = `已核对 ${data.articles_read} 项，新增 ${data.new_articles} 项会员链接记录。`;
  } catch {
    result.textContent = '导入结果未返回，请重试。';
  } finally {
    save.disabled = false;
  }
});

const readWorklist = document.getElementById('load-member-worklist');
const worklist = document.getElementById('member-worklist');
const worklistResult = document.getElementById('member-worklist-result');
const limit = document.getElementById('member-limit');
const resume = document.getElementById('member-resume');
const evidence = document.getElementById('member-evidence');
const advance = document.getElementById('member-advance');
const saveProgress = document.getElementById('save-member-progress');
const progressResult = document.getElementById('member-progress-result');

readWorklist.addEventListener('click', async () => {
  worklist.value = '';
  if (!limit.validity.valid || !limit.value) {
    worklistResult.textContent = '请输入 1 到 128 的数量。';
    return;
  }
  const resumeHistory = resume.checked;
  const query = new URLSearchParams({limit: limit.value, resume: resumeHistory ? '1' : '0'});
  readWorklist.disabled = true;
  worklistResult.textContent = '正在读取待采项目…';
  try {
    const response = await fetch(`/api/member-worklist?${query}`);
    const data = await response.json();
    if (!response.ok) {
      worklistResult.textContent = data.error || '读取未完成，请重试。';
      return;
    }
    worklist.value = JSON.stringify(data);
    advance.checked = resumeHistory;
    worklistResult.textContent = data.items.length ? `已读取 ${data.items.length} 个待采项目。` : '这里已没有待采项目。';
  } catch {
    worklistResult.textContent = '读取未完成，请重试。';
  } finally {
    readWorklist.disabled = false;
  }
});

saveProgress.addEventListener('click', async () => {
  let report;
  try {
    report = JSON.parse(evidence.value);
  } catch {
    progressResult.textContent = '请检查本批核对结果的格式。';
    return;
  }
  saveProgress.disabled = true;
  progressResult.textContent = '正在保存本批核对结果…';
  try {
    const response = await fetch('/api/member-progress', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({advance: advance.checked, report})
    });
    const data = await response.json();
    if (!response.ok) {
      progressResult.textContent = data.error || '保存未完成，请重试。';
      return;
    }
    evidence.value = '';
    worklist.value = '';
    worklistResult.textContent = '';
    progressResult.textContent = `已保存 ${data.inspected} 个项目的核对结果。`;
  } catch {
    progressResult.textContent = '保存结果未返回。请保留本批结果，稍后重试。';
  } finally {
    saveProgress.disabled = false;
  }
});
