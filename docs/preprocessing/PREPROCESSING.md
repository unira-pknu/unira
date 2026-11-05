# 전체 수집 자료 전처리

현재 수집한 학과·본교·강의계획서·규정 자료를 점검하고 RAG용 청크 JSON을
생성하는 실행 안내입니다. 모든 명령은 **프로젝트 루트의 PowerShell**에서
Python 3.12 가상환경으로 실행합니다.

```powershell
.\.venv\Scripts\python.exe --version
```

## 1. 전체 범위와 실행 환경 점검

```powershell
# 데이터셋 수, 형식별 파일 수, 기본 의존성과 Tesseract 경로 확인
.\.venv\Scripts\python.exe -m scripts.preprocessing.collected

# 데이터셋별 세부 집계도 표시
.\.venv\Scripts\python.exe -m scripts.preprocessing.collected --details
```

위 명령은 읽기 전용입니다. JSON을 읽어 문서 여부를 확인하므로 파일이 많으면
시간이 걸립니다. `extractable`은 추출기가 지원하는 확장자라는 뜻이며,
문서의 손상·암호화·인코딩·본문 품질까지 검증한 결과는 아닙니다.

| 점검 분류 | 의미 |
|---|---|
| `document_json` | `type`과 `url`이 있는 수집 JSON. 본문 처리 대상 |
| `extractable` | 공통 추출기가 지원하는 문서·첨부·ZIP |
| `html_snapshot` | 저장된 HTML. 공통 처리에서 제외하며 규정은 별도 처리 |
| `image_separate_review` | 별도 검토가 필요한 이미지 |
| `other_json` | 수집 본문으로 식별되지 않은 JSON |
| `unsupported` | 현재 공통 추출기가 지원하지 않는 형식 |

필요한 기본 패키지는 PyMuPDF(`fitz`), python-docx, xlrd, python-pptx,
pyhwp, beautifulsoup4입니다. XLSX 추출은 표준 ZIP/XML 모듈을 사용하므로
openpyxl이 필요하지 않습니다. DOC 변환에는 pywin32와 Microsoft Word가 필요하며,
현재 구현은 원본 옆에 DOCX를 생성합니다.

스캔 PDF도 처리하려면 다음 명령에서 Tesseract 실행 파일과 `kor`, `eng`
언어 데이터가 확인되어야 합니다.

Windows에서는 먼저 다음 명령으로 엔진을 설치합니다.

```powershell
winget install --id UB-Mannheim.TesseractOCR --exact --source winget
```

설치 폴더를 PATH에 추가한 뒤 새 터미널을 여세요. `kor` 언어가 없다면
[공식 언어 데이터](https://github.com/tesseract-ocr/tessdata_fast)를 사용해
`kor.traineddata`를 설치 폴더의 `tessdata/`에 추가해야 합니다.

```powershell
Get-Command tesseract
tesseract --list-langs
```

Tesseract를 설치한 폴더가 PATH에 있어야 PDF 추출기가 찾을 수 있습니다.
읽기 전용 집계의 의존성 확인만으로 Word 실행이나 OCR 언어 설치까지 보장하지는 않습니다.

## 2. 처리 경로 미리 확인

```powershell
.\.venv\Scripts\python.exe -m scripts.preprocessing.collected --dry-run
```

청크는 생성하지 않고 처리할 파일을 나열합니다. 로그는
`logs/preprocessing_pipeline.log`에 기록합니다. 최신 결과가 있는 일반 파일은
건너뛰므로, 출력 목록이 전체 원본 개수와 같을 필요는 없습니다.

## 3. 전체 전처리 실행

전처리 결과는 모두 `files/preprocessed/<dataset>/` 아래로 모읍니다.
수집 원본은 기존 위치에 유지하며, 벡터화 도구의 기본 입력 경로도 이 위치를 사용합니다.
공통 CLI에 `--output-root`를 직접 지정하면 해당 경로가 우선합니다.

```powershell
.\.venv\Scripts\python.exe -m scripts.preprocessing.collected --run
```

등록된 학과 중 `output/`이 존재하는 데이터셋과 `files/pknu_main/`,
`files/pknu_syllabus/`, `files/rule/output/`을 대상으로 처리합니다.
크롤링·벡터화·DB 적재는 수행하지 않습니다.

| 수집 자료 | 전처리 출력 루트 |
|---|---|
| 학과·단과대학 | `files/department/<레지스트리 저장 경로>/<dataset>/preprocessed/` |
| 본교 전체 | `files/preprocessed/pknu_main/` |
| 강의계획서 | `files/preprocessed/pknu_syllabus/` |
| 규정 | `files/preprocessed/rule/` |

본문 JSON은 `json/`, 첨부파일은 `files/` 아래에 원본의 상대 경로를 반영합니다.
규정은 전용 처리기의 JSON·HTML·첨부 출력 구조를 사용합니다.
기본 청크 크기는 900자, 겹침은 120자이며 공통 PDF OCR은 `auto`, 언어는
`kor+eng`, 렌더링 해상도는 200 DPI입니다.

텍스트 PDF만 우선 처리하려면 다음처럼 실행할 수 있습니다.

```powershell
.\.venv\Scripts\python.exe -m scripts.preprocessing.collected --run --pdf-ocr never
```

`--pdf-ocr auto|never|always`는 공통 파일 처리에 적용됩니다. **규정 전용
처리기에는 이 옵션이 전달되지 않으며**, 규정 첨부는 기존 추출 기본값을 사용합니다.
`--dry-run`과 `--run`을 함께 지정하면 미리보기로 동작합니다.

## 4. 중단 후 재실행과 강제 재처리

중단한 작업은 같은 전체 실행 명령으로 다시 시작합니다.

```powershell
.\.venv\Scripts\python.exe -m scripts.preprocessing.collected --run
```

일반 파일은 입력·관련 메타데이터보다 출력이 최신이면 건너뜁니다. ZIP은
최신 여부 건너뛰기 대상에서 제외되므로 재실행 시 다시 처리합니다.
이 검사는 수정 시각 기반이며 결과의 유효성이나 추출 옵션 변경을 비교하지 않습니다.
OCR 환경을 보완하거나 이름 복원 코드를 바꿨다면 `--force`로 전체를 재처리할 수 있습니다.

```powershell
.\.venv\Scripts\python.exe -m scripts.preprocessing.collected --run --force --console
```

4개 터미널에서는 각각 `--workers 4 --worker-index 0`부터 `--worker-index 3`까지
서로 다른 인덱스를 지정합니다. 같은 작업의 `--workers` 값은 모두 같아야 합니다.
작업별 `--log-file logs/<작업명>.log`를 사용하면 로그를 분리할 수 있습니다.
`--console`은 로그 파일과 터미널에 함께 진행 상황을 출력합니다.
`DATASET-START`는 작업 내 데이터셋 진행도이며, `PROGRESS`는 현재 입력 폴더의
파일 진행도입니다. ZIP은 압축파일 1개를 파일 1건으로 계산하고 내부 멤버는 별도 로그로 표시합니다.

다음 예시는 기존 전체 실행과 같은 강의계획서 출력 위치에 PDF를 다시 생성합니다.
`--changed-only`를 생략하면 기존 최신 결과도 다시 처리합니다.

```powershell
.\.venv\Scripts\python.exe -m scripts.preprocessing.preprocess_files `
  --input-root files/pknu_syllabus `
  --output-root files/preprocessed/pknu_syllabus/files `
  --file-ext pdf `
  --layout by_ext `
  --pdf-ocr auto
```

입력 루트 아래의 PDF만 선택하므로 이 예시에서는 출력 JSON이 다시 입력으로
들어가지 않습니다. 임의의 루트를 지정할 때는 파생 결과가 입력 범위에 섞이지 않도록
주의하세요. 공통 CLI는 `collected`의 운영·파생 폴더 제외 규칙을 적용하지 않습니다.

## 5. 일부 데이터셋·파일 형식 처리

```powershell
# 학과의 레지스트리 저장 경로를 자동으로 찾아 본문과 첨부 처리
.\.venv\Scripts\python.exe -m scripts.preprocessing.preprocessing_pipeline --dataset ce --dry-run
.\.venv\Scripts\python.exe -m scripts.preprocessing.preprocessing_pipeline --dataset ce --changed-only

# 규정 자료만 처리
.\.venv\Scripts\python.exe -m scripts.preprocessing.preprocessing_pipeline --dataset rule --changed-only
```

`ce` 대신 등록된 다른 학과 별칭을 사용할 수 있습니다.
`preprocessing_pipeline --dataset all`은 CE·공지사항·대학생활·규정의 기존
4개 범위만 처리합니다. **전체 수집 자료 처리는 `collected --run`을 사용하세요.**
`preprocessing_pipeline_recent`는 크롤링·벡터화·DB 적재도 포함하므로
전처리만 수행하는 명령과 구분해야 합니다.

임의의 첨부 폴더는 공통 CLI로 처리합니다. 아래 경로는 실제 경로로 바꿉니다.

```powershell
.\.venv\Scripts\python.exe -m scripts.preprocessing.preprocess_files `
  --input-root '<원본 첨부 폴더>' `
  --output-root '<전처리 출력 폴더>' `
  --output-json-root '<대응하는 수집 본문 JSON 폴더>' `
  --file-ext pdf hwp hwpx xls xlsx docx pptx zip `
  --changed-only
```

`--output-json-root`를 생략하면 첨부 자체 경로 기반 출처만 사용합니다.
수집 본문 JSON을 직접 처리할 때는 `--layout flat`을 사용합니다.
공통 CLI의 `--chunk-size`, `--chunk-overlap`, `--ocr-language`, `--ocr-dpi`로
추출·청크 설정을 조정할 수 있습니다.

## 6. 로그와 결과 확인

```powershell
Get-Content logs/preprocessing_pipeline.log -Tail 50
Select-String -Path logs/preprocessing_pipeline.log -Pattern '\[FAIL','\[OCR-SKIP\]','\[OCR-FAIL\]'
```

전체 실행은 `logs/preprocessing_pipeline.log`, 공통 CLI는
`logs/preprocess_files.log`를 사용합니다. 파일은 누적되므로 실행 시각도 확인하세요.

`[OK]`만으로 본문 추출 성공을 판단하지 마세요. 공통 결과 JSON의
`used_metadata_fallback`, `num_chunks`, `total_chars`, `chunks` 내용을 확인해야 합니다.
본문이 없으면 메타데이터만 청크로 생성될 수 있으며 ZIP 내부 일부 실패도
확인해야 합니다. 개별 파일 실패가 전체 프로세스의 실패 종료 코드로 이어지지
않을 수 있으므로 종료 코드와 함께 로그를 확인합니다.

공통 파일 처리 로그의 마지막 상태가 `[FAIL]`인 파일만 다시 처리할 수 있습니다.
동일한 입력·출력 루트와 레이아웃을 사용하고 `--changed-only`는 생략합니다.

```powershell
.\.venv\Scripts\python.exe -m scripts.preprocessing.preprocess_files `
  --input-root '<실패 파일이 있는 원본 루트>' `
  --output-root '<기존 전처리 출력 루트>' `
  --output-json-root '<대응하는 수집 본문 JSON 폴더>' `
  --failed-from-log logs/preprocessing_pipeline.log
```

이 재시도 옵션은 공통 처리기의 파일 단위 `[FAIL]`에 대한 기능입니다.
규정 전용 로그, ZIP 멤버별 실패, 메타데이터만 생성된 결과는 별도 확인이 필요합니다.

## 처리 범위의 한계

- 상태·이력·파생 결과 폴더는 전체 실행에서 제외합니다.
- 일반 HTML은 수집 JSON과 중복되어 공통 실행에서 제외하고 규정은 별도로 처리합니다.
- 이미지·영상·구형 PPT·EGG·XLSB 등은 전체 실행으로 자동 추출하지 않습니다.
- ZIP은 지원 멤버만 처리하며 멤버 수·크기 제한이 있습니다.
- ZIP 내부 파일명은 UTF-8 표시를 우선하며, 표시가 없는 한글 파일명은 CP949로
  복원합니다. ZIP 재처리 시 정상 결과 생성 후 출처가 일치하는 기존 깨진 이름의
  결과 JSON을 제거합니다. 원본 ZIP은 변경하지 않습니다.
- 본교 본문 이미지 OCR 금지 정책은 유지됩니다. PDF OCR과는 별도 정책입니다.
- 강의계획서는 PDF 경로를 출처로 보존하며 과목 목록 메타데이터 조인은 아직 없습니다.
- 본교 전체·강의계획서 결과의 벡터화와 DB 등록은 별도 작업입니다.

구현과 모듈 설명은 [전처리 패키지 README](../../scripts/preprocessing/README.md),
추가 옵션은 [기존 개별 명령 안내](../commands/PREPROCESSING.md)를 참고하세요.
