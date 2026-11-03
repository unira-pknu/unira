# 프로젝트 문서

문서는 기능별 폴더에서 관리합니다. 명령어의 실행 위치는 별도 표기가 없으면 프로젝트 루트입니다.
저장소 루트의 [README](../README.md)에서 이 목차로 들어올 수 있습니다.

| 기능 | 문서 | 내용 |
| --- | --- | --- |
| 프로젝트 | [개요·초기 설정](project/README.md) | 시스템 구성, 설치, 전체 파이프라인 |
| 프로젝트 | [요구사항](project/PRD.md) | 제품 목표와 기능 요구사항 |
| 명령어 | [명령어 목차](commands/README.md) | 크롤링부터 평가까지 실행 흐름 |
| 명령어 | [크롤링](commands/CRAWLERS.md) | 크롤러·마이그레이션·설정 탐색 명령 |
| 명령어 | [전처리](commands/PREPROCESSING.md) | 문서·첨부파일 전처리 |
| 명령어 | [벡터 DB·RAG](commands/VECTOR_DATABASE.md) | 벡터화, 적재, 검색, 파이프라인 |
| 명령어 | [평가 실행](commands/EVALUATION.md) | 질문 세트와 API 평가 명령 |
| 크롤러 | [전체 가이드](crawlers/README.md) | 공통 설치, 전체·개별 실행 |
| 크롤러 | [메인 홈페이지](crawlers/MAIN.md) | 페이지·카테고리별 수집과 정책 |
| 크롤러 | [학과·단과대학](crawlers/DEPARTMENTS.md) | 전체 대상, 실행, 저장 구조, TLS, 보류 사유 |
| 크롤러 | [TLS 인증서](crawlers/TLS_CERTIFICATES.md) | 중간 인증서 출처와 호스트 정책 |
| 백엔드 | [백엔드 가이드](backend/README.md) | 환경변수, API 실행과 동작 |
| 백엔드 | [학교 서버 운영](backend/SCHOOL_SERVER.md) | 운영 백엔드 업데이트·재시작·되돌리기 |
| 프론트엔드 | [프론트엔드 가이드](frontend/README.md) | 개발 서버와 Supabase 공개 설정 |
| 데이터 | [데이터 구조](data/STRUCTURE.md) | 수집 스키마, 저장 구조와 마이그레이션 |
| 평가 | [평가 데이터·절차](evaluation/README.md) | 질문 세트, 라벨 검수와 평가 결과 |
| 평가 | [기준점 관리](evaluation/BASELINES.md) | baseline 파일과 기록 규칙 |
| 테스트 | [테스트 가이드](testing/README.md) | 테스트 구조·추가 기준·실행 |
| 개발 | [스크립트 구조](development/SCRIPTS.md) | 코드 경로, 공통 모듈과 책임 |

## 문서 관리

기능별 설명은 해당 폴더에 추가하고, 이 목차와 관련 문서의 링크를 갱신합니다.
명령어는 프로젝트 루트 기준 경로로 적고, 문서 링크는 문서 위치 기준 상대 경로를 사용합니다.
코드·설정·데이터 파일은 원래 경로에 두고 문서에서 링크합니다.
`AGENTS.md`와 `CLAUDE.md` 같은 도구용 작업 지침은 도구가 읽는 위치에서 관리합니다.
