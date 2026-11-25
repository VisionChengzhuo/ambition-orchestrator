#!/usr/bin/env bash
# build/run-task.sh <T0..T5> | --selftest   一次性构建脚本：写测试 → 实现 → 验收 → 异家复核 → 合入 main
set -u
cd "$HOME/amb"
B=build
declare -A TESTER=( [T1]=sol [T2]=opus [T3]=sol [T4]=opus [T5]=sol )
declare -A IMPL=( [T0]=sol [T1]=opus [T2]=sol [T3]=opus [T4]=sol [T5]=opus )
declare -A REVIEWER=( [T1]=sol [T2]=opus [T3]=sol [T4]=opus [T5]=sol )
declare -A ACCEPT=(
 [T0]='bash build/run-task.sh --selftest && test -s build/crontab.before'
 [T1]='python3 -m unittest tests.test_t1 -v'
 [T2]='python3 -m unittest tests.test_t1 tests.test_t2 -v'
 [T3]='python3 -m unittest tests.test_t1 tests.test_t2 tests.test_t3 -v'
 [T4]="python3 -m unittest discover -s tests -p 'test_t[1-4].py' -v"
 [T5]="python3 -m unittest discover -s tests -v && bash install-cron.sh && bash install-cron.sh && test \"\$(crontab -l | grep -c 'amb.py tick')\" = 1 && diff <(crontab -l | grep -v 'amb.py tick') build/crontab.before"
)

if [ "${1:-}" = "--selftest" ]; then
  for f in exec_ok exec_bad exec_hang exec_touch_tests exec_garbage review_pass review_fail review_wrong_sha plan_two_subs; do
    test -x tests/fakes/$f.sh || { echo "missing/not executable: $f.sh"; exit 1; }
  done
  d=$(mktemp -d); mkdir -p $d/w; echo "SHA: abc1234" > $d/p; echo "REPO: /x" >> $d/p
  tests/fakes/exec_ok.sh $d/w $d/p $d/o && test -s $d/w/DONE.txt && python3 -c "import json;json.load(open('$d/o'))" || exit 1
  tests/fakes/review_pass.sh $d/w $d/p $d/o && python3 -c "import json;assert json.load(open('$d/o'))['sha']=='abc1234'" || exit 1
  tests/fakes/review_wrong_sha.sh $d/w $d/p $d/o && python3 -c "import json;assert json.load(open('$d/o'))['sha']=='deadbeef'" || exit 1
  tests/fakes/plan_two_subs.sh $d/w $d/p $d/o && python3 -c "import json;assert len(json.load(open('$d/o'))['subs'])==2" || exit 1
  tests/fakes/exec_garbage.sh $d/w $d/p $d/o; ! python3 -c "import json;json.load(open('$d/o'))" 2>/dev/null || exit 1
  echo selftest ok; exit 0
fi

T="$1"; n=$(echo $T | tr 'T' ' ' | tr -d ' ')
OUT=$B/$T; mkdir -p $OUT
verdict() { printf '{"task":"%s","state":"%s","detail":"%s","ts":"%s"}\n' "$T" "$1" "$2" "$(date -Is)" > $OUT/verdict.json; echo "$T $1: $2"; }

run_role() { # model mode prompt out
  local model=$1 mode=$2 prompt=$3 out=$4
  if [ "$model" = sol ]; then
    local sb=(--dangerously-bypass-approvals-and-sandbox)  # VPS 上 codex 沙箱(bwrap)不可用；ro 靠事后 git status 校验
    timeout 1800 codex exec "${sb[@]}" -m gpt-6.2-sol -c model_reasoning_effort=medium -C "$HOME/amb" --skip-git-repo-check -o "$out" - < "$prompt" > "$out.log" 2>&1
  else
    local pm=(--dangerously-skip-permissions); [ "$mode" = ro ] && pm=(--permission-mode plan)
    timeout 1800 claude -p --model opus --effort medium "${pm[@]}" --no-session-persistence < "$prompt" > "$out" 2> "$out.log"
  fi
}

git checkout -q main || exit 1
if [ -n "${REVIEW_ONLY:-}" ]; then git checkout -q t/$n; else git checkout -q -B t/$n; fi
COMMON="项目：~/amb 是 Ambition 派活编排器（Python3 标准库单文件 amb.py，cron tick）。先完整阅读 ~/amb/build/PLAN.md（A 接口、B 子任务单、Conventions），你的任务是其中的 $T。不要碰 <REDACTED_PATH> 和 crontab；不要访问网络；不要泄露任何凭据。"

if [ "$T" = T0 ]; then
  echo "$COMMON 实现 T0：写 tests/fakes/ 下的 9 个假脚本（可执行，bash），遵循 Conventions；完成后运行验收 \`${ACCEPT[T0]}\`。build/run-task.sh 不要改。最后输出一行 DONE。" > $OUT/impl.prompt
  run_role sol write $OUT/impl.prompt $OUT/impl.out
  bash -c "${ACCEPT[T0]}" > $OUT/accept.log 2>&1 && { git add -A; git commit -qm "T0 fakes" ; git checkout -q main; git merge -q --ff-only t/0; verdict PASS "selftest ok"; exit 0; } || { verdict FAIL "selftest failed"; exit 1; }
fi

tester_step() {
echo "$COMMON 你是【测试作者】(${TESTER[$T]})：只创建/修改 tests/ 下的 test_t${n}.py（及必要的 tests/__init__.py、tests/fakes 之外的辅助文件）；黑盒测试，通过 subprocess 调用 python3 ~/amb/amb.py；用例必须覆盖 PLAN.md 中 $T 的“必测用例”全部条目；不要写 amb.py。写完后运行 python3 -m unittest tests.test_t$n 确认测试文件语法可导入（此时因功能未实现而失败是预期的）。最后输出一行 DONE。" > $OUT/test.prompt
run_role ${TESTER[$T]} write $OUT/test.prompt $OUT/test.out
git add -A tests; git commit -qm "$T tests (${TESTER[$T]})" || { verdict FAIL "tester produced no tests"; exit 1; }
}

implement_step() { # $1=extra prompt ; sets ok, i
  local prev="$1" k
  for k in 1 2 3; do
    i=$((i+1))
    echo "$COMMON 你是【实现者】(${IMPL[$T]})：在 ~/amb/amb.py 实现/扩展功能（只用标准库、单文件），使验收通过：\`${ACCEPT[$T]}\`。严禁修改 tests/ 下任何文件（会被 diff 检查并回滚）。T5 还需创建 install-cron.sh 与 README.md。完成后自己运行验收，不通过就继续改。最后输出一行 DONE。$prev" > $OUT/impl$i.prompt
    run_role ${IMPL[$T]} write $OUT/impl$i.prompt $OUT/impl$i.out
    git checkout -q $TESTSHA -- tests 2>/dev/null
    git status --porcelain tests | grep -q . && git clean -fdq tests
    bash -c "${ACCEPT[$T]}" > $OUT/accept$i.log 2>&1 && { ok=1; break; } || ok=0
    prev="$1 上一轮验收失败，输出末尾如下，请修复：$(tail -c 3000 $OUT/accept$i.log)"
  done
  git add -A; git commit -qm "$T impl (${IMPL[$T]}) round $i" -q
  [ "$ok" = 1 ] || { verdict FAIL "acceptance failed after retries"; git checkout -q main; exit 1; }
  git diff --quiet $TESTSHA HEAD -- tests || { verdict FAIL "tests modified by implementer"; git checkout -q main; exit 1; }
}

review_step() { # returns 0 if PASS
  SHA=$(git rev-parse HEAD)
  DIFF=$(git diff main..HEAD -- . ":(exclude)tests" ":(exclude)build" | head -c 80000)
  { echo "$COMMON 你是【只读复核者】(${REVIEWER[$T]})，不得修改任何文件。对照 PLAN.md 的 $T 与接口，审查下面内联的 diff 与验收日志 $OUT/accept$i.log：是否满足需求、有无偏离接口、安全问题（shell 注入、符号链接越权读取、凭据泄露）、测试是否被绕过。只报告真实、可复现、与 $T 范围相关的缺陷；风格问题不算 FAIL。范围规定：只有（a）违反 PLAN.md 中 $T 的接口或必测用例语义、（b）现实可达的安全问题（shell/命令注入、凭据泄露）、（c）会导致任务状态错误或丢失的逻辑缺陷，才可判 FAIL；极端文件系统构造（FIFO、符号链接目录、并发竞争的极小窗口、人为构造的非法 JSON 类型）一律作为 notes 放进 findings 但 verdict 仍给 PASS。"; echo "SHA: $SHA"; echo "以下是 diff（已内联，不需要运行命令；可只读查看文件）："; echo "$DIFF"; echo "最后一行只输出 JSON：{\"verdict\":\"PASS\"或\"FAIL\",\"sha\":\"$SHA\",\"findings\":[字符串...]}"; } > $OUT/review$R.prompt
  run_role ${REVIEWER[$T]} ro $OUT/review$R.prompt $OUT/review$R.out
  if [ -n "$(git status --porcelain)" ]; then git checkout -q -- . ; git clean -fdq; echo '{"verdict":"FAIL","sha":"x","findings":["reviewer modified files"]}' > $OUT/review$R.out; fi
  python3 - "$OUT/review$R.out" "$OUT/review.json" <<'PYEOF'
import sys,json
t=open(sys.argv[1],errors="replace").read()
d=None
dec=json.JSONDecoder()
i=t.rfind('{"verdict"')
while i!=-1 and d is None:
    try: d,_=dec.raw_decode(t[i:])
    except Exception: i=t.rfind('{"verdict"',0,i)
json.dump(d or {'verdict':'FAIL','findings':['unparseable']},open(sys.argv[2],'w'),ensure_ascii=False)
PYEOF
  python3 -c "import json,sys;d=json.load(open('$OUT/review.json'));sys.exit(0 if d.get('verdict')=='PASS' and d.get('sha','')[:7]=='$SHA'[:7] else 1)"
}

i=0; ok=0
if [ -z "${REVIEW_ONLY:-}" ]; then
  tester_step
  TESTSHA=$(git rev-parse HEAD)
  implement_step ""
else
  TESTSHA=$(git log --format=%H --grep="^$T tests" -1); i=$(ls $OUT/accept*.log | wc -l); ok=1
fi
for R in 1 2; do
  if review_step; then
    git checkout -q main && git merge -q --ff-only t/$n && verdict PASS "merged $SHA after $R review round(s)"; exit 0
  fi
  [ "$R" = 2 ] && break
  implement_step "异家复核者未通过，发现如下，请逐条核实并修复真实问题（对误报可在代码注释中说明理由）：$(python3 -c "import json;print(json.dumps(json.load(open('$OUT/review.json')).get('findings'),ensure_ascii=False))")"
done
# 构建期策略：验收已过但复核 2 轮仍有意见 → 带未解决意见合入，并在 verdict 中标注，交人最终判断
cp $OUT/review.json $OUT/unresolved-findings.json
git checkout -q main && git merge -q --ff-only t/$n && verdict MERGED_WITH_FINDINGS "unresolved reviewer findings in $OUT/unresolved-findings.json"; exit 0
