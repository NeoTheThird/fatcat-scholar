
import os
import io
import sys
import argparse
from pydantic import BaseModel, validator
from typing import List, Dict, Tuple, Optional, Any, Sequence
from fatcat_openapi_client import ReleaseEntity, FileEntity
import internetarchive

from fatcat_scholar.api_entities import *
from fatcat_scholar.djvu import djvu_extract_leaf_texts
from fatcat_scholar.sandcrawler import SandcrawlerPostgrestClient, SandcrawlerMinioClient
from fatcat_scholar.issue_db import IssueDB, SimIssueRow
from fatcat_scholar.schema import *
from fatcat_scholar.grobid2json import teixml2json


def es_fulltext_from_sim(sim: Dict[str, Any]) -> Optional[ScholarFulltext]:
    if not sim['page_texts']:
        return None
    first_page = sim['page_texts'][0]['page_num']
    issue_item = sim['issue_item']
    return ScholarFulltext(
        lang_code=None, # TODO: pub/issue metadata? or langdetect?
        body="\n".join([p['raw_text'] for p in sim['page_texts']]),
        #acknowledgement=None,
        #annex=None,
        release_ident=sim.get('release_ident'),
        #file_ident=None,
        #file_sha1=None,
        #file_mimetype=None,
        thumbnail_url=f"https://archive.org/serve/{issue_item}/__ia_thumb.jpg",
        access_url=f"https://archive.org/details/{issue_item}/page/{first_page}",
        access_type=AccessType.ia_sim,
    )

def es_sim_from_sim(sim: Dict[str, Any]) -> ScholarSim:
    first_page = None
    if sim['page_texts']:
        first_page = sim['page_texts'][0]['page_num']
    return ScholarSim(
        issue_item=sim['issue_item'],
        pub_collection=sim['pub_item_metadata']['metadata']['identifier'],
        sim_pubid=sim['issue_item_metadata']['metadata']['sim_pubid'],
        first_page=first_page,
    )

SIM_RELEASE_TYPE_MAP = {
    'Scholarly Journals': 'article-journal',
    # TODO:
}
SIM_LANG_MAP = {
    'English': 'en',
    # TODO:
}
SIM_COUNTRY_MAP = {
    'Netherlands': 'nl',
    # TODO:
}

def es_biblio_from_sim(sim: Dict[str, Any]) -> ScholarBiblio:

    issue_meta = sim['issue_item_metadata']['metadata']
    pub_meta = sim['pub_item_metadata']['metadata']

    first_page = None
    if sim['page_texts']:
        first_page = sim['page_texts'][0]['page_num']
    container_name = sim['pub_item_metadata']['metadata']['title']
    last_word = container_name.split()[-1]
    if len(last_word) == 9 and last_word[4] == '-':
        container_name = container_name[:-10]

    issns = []
    raw_issn = issue_meta.get('issn')
    if raw_issn and len(raw_issn) == 9:
        issns.append(raw_issn)

    volume = issue_meta.get('volume')
    volume_int = None
    if volume and volume.isdigit():
        volume_int = int(volume)
    issue = issue_meta.get('issue')
    issue_int = None
    if issue and issue.isdigit():
        issue_int = int(issue)

    date = issue_meta.get('date')
    release_year = None
    if date and len(date) > 4 and date[:4].isdigit():
        release_year = int(date[:4])

    release_date = None
    if len(date) == len("2000-01-01"):
        release_date = date

    return ScholarBiblio(
        #release_ident=release.ident,
        title=None,
        #subtitle=None,
        #original_title=release.original_title,
        release_date=release_date,
        release_year=release_year,
        release_type=SIM_RELEASE_TYPE_MAP.get(pub_meta.get('pub_type')),
        release_stage="published", # as a default
        #withdrawn_status=release.withdrawn_status,
        lang_code=SIM_LANG_MAP.get(pub_meta.get('language')),
        country_code=SIM_COUNTRY_MAP.get(pub_meta.get('country')),
        volume=volume,
        volume_int=volume_int,
        issue=issue,
        issue_int=issue_int,
        pages=sim.get('pages'),
        first_page=first_page,
        first_page_int=None,
        #number=None,

        # no external identifiers

        #license_slug=release.license_slug,
        publisher=issue_meta.get('publisher'),
        container_name=container_name,
        container_original_name=None, # TODO pass-through
        container_ident=None, # TODO: pass-through
        container_type=None, # TODO
        container_issnl=None, # TODO: pass-through
        issns=issns,

        # no contrib/affiliation info
        contrib_names=[],
        affiliations=[],
    )

def _add_file_release_meta(fulltext: ScholarFulltext, re: ReleaseEntity, fe: FileEntity) -> ScholarFulltext:
    best_url = None
    best_url_type = None
    for url in fe.urls:
        best_url = url.url
        best_url_type = AccessType.web
        if '//archive.org/' in url.url:
            best_url_type = AccessType.ia_file
            break
        elif '//web.archive.org/' in url.url:
            best_url_type = AccessType.wayback
            break
        if url.rel == "repository":
            best_url_type = AccessType.repository
        # TODO: more file-to-access logic

    fulltext.release_ident = re.ident
    fulltext.file_ident = fe.ident
    fulltext.file_sha1 = fe.sha1
    fulltext.file_mimetype = fe.mimetype
    fulltext.access_url = best_url
    fulltext.access_type = best_url_type
    return fulltext


def es_fulltext_from_grobid(tei_xml: str, re: ReleaseEntity, fe: FileEntity) -> Optional[ScholarFulltext]:
    obj = teixml2json(tei_xml)
    ret = ScholarFulltext(
        lang_code=obj.get('lang'),
        body=obj.get('body'),
        acknowledgement=obj.get('acknowledgement'),
        annex=obj.get('annex'),
        thumbnail_url=None, # TODO: sandcrawler thumbnails
    )
    return _add_file_release_meta(ret, re, fe)

def es_fulltext_from_pdftotext(pdftotext: Any, re: ReleaseEntity, fe: FileEntity) -> Optional[ScholarFulltext]:

    ret = ScholarFulltext(
        lang_code=re.language,
        body=pdftotext['raw_text'],
        acknowledgement=None,
        annex=None,
        thumbnail_url=None, # TODO: sandcrawler thumbnails
    )
    return _add_file_release_meta(ret, re, fe)

def transform_heavy(heavy: IntermediateBundle) -> Optional[ScholarDoc]:

    tags: List[str] = []
    work_ident: Optional[str] = None
    abstracts: List[ScholarAbstract] = []
    fulltext: Optional[ScholarFulltext]

    ia_sim: Optional[ScholarSim] = None
    if heavy.sim_fulltext is not None:
        ia_sim = es_sim_from_sim(heavy.sim_fulltext)

    if heavy.doc_type == DocType.sim_page:
        assert ia_sim is not None
        key = f"page_{ia_sim.issue_item}_{ia_sim.first_page}"
        biblio = es_biblio_from_sim(heavy.sim_fulltext)
        fulltext = es_fulltext_from_sim(heavy.sim_fulltext)
    elif heavy.doc_type == DocType.work:
        work_ident = heavy.releases[0].work_id
        key = f"work_{work_ident}"
        assert heavy.biblio_release_ident
        primary_release = [r for r in heavy.releases if r.ident == heavy.biblio_release_ident][0]
        biblio = es_biblio_from_release(primary_release)

        # TODO: abstracts from releases also; abstracts_dict; abstracts from GROBID parse
        abstracts = es_abstracts_from_release(primary_release)
    else:
        raise NotImplementedError(f"doc_type: {heavy.doc_type}")

    if heavy.grobid_fulltext:
        
        fulltext_release = [r for r in heavy.releases if r.ident == heavy.grobid_fulltext['release_ident']][0]
        fulltext_file = [f for f in fulltext_release.files if f.ident == heavy.grobid_fulltext['file_ident']][0]

        fulltext = es_fulltext_from_grobid(heavy.grobid_fulltext['tei_xml'], fulltext_release, fulltext_file)

        # hack to pull through thumbnail from local pdftotext
        if fulltext and fulltext.file_sha1 and not fulltext.thumbnail_url and heavy.pdftotext_fulltext:
            # https://covid19.fatcat.wiki/fulltext_web/thumbnail/c9/c9e87f843b3cf7dc47881fa3d3ccb4693d7d9521.png
            fulltext.thumbnail_url = f"https://covid19.fatcat.wiki/fulltext_web/thumbnail/{fulltext.file_sha1[:2]}/{fulltext.file_sha1}.png"

    if not fulltext and heavy.pdftotext_fulltext:

        fulltext_release = [r for r in heavy.releases if r.ident == heavy.grobid_fulltext['release_ident']][0]
        fulltext_file = [f for f in fulltext_release.files if f.ident == heavy.grobid_fulltext['file_ident']][0]
        fulltext = es_fulltext_from_pdftotext(heavy.pdftotext_fulltext, fulltext_release, fulltext_file)

    # TODO: additional access list
    access_dict = dict()
    if fulltext and fulltext.access_type:
        access_dict[fulltext.access_type] = ScholarAccess(
            access_type=fulltext.access_type,
            access_url=fulltext.access_url,
            mimetype=fulltext.file_mimetype,
            file_ident=fulltext.file_ident,
            release_ident=fulltext.release_ident,
        )
    if ia_sim and not AccessType.ia_sim in access_dict:
        access_dict[AccessType.ia_sim] = ScholarAccess(
            access_type=AccessType.ia_sim,
            access_url=f"https://archive.org/details/{ia_sim.issue_item}/page/{ia_sim.first_page}",
        )

    # TODO: additional abstracts

    return ScholarDoc(
        key=key,
        doc_type=heavy.doc_type.value,
        doc_index_ts=datetime.datetime.utcnow(),
        work_ident=work_ident,
        tags=tags,

        biblio=biblio,
        fulltext=fulltext,
        ia_sim=ia_sim,
        abstracts=abstracts,
        releases=[es_release_from_release(r) for r in heavy.releases],
        access=list(access_dict.values()),
    )

def run_transform(infile):
    for line in infile:
        obj = json.loads(line)

        heavy = IntermediateBundle(
            doc_type=DocType(obj['doc_type']),
            releases=[entity_from_json(json.dumps(re), ReleaseEntity) for re in obj['releases']],
            biblio_release_ident=obj.get('biblio_release_ident'),
            grobid_fulltext=obj.get('grobid_fulltext'),
            pdftotext_fulltext=obj.get('pdftotext_fulltext'),
            sim_fulltext=obj.get('sim_fulltext'),
        )
        es_doc = transform_heavy(heavy)
        if not es_doc:
            continue
        print(es_doc.json())

def main():
    """
    Run this command like:

        python -m fatcat_scholar.transform
    """

    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    subparsers = parser.add_subparsers()

    sub = subparsers.add_parser('run_transform',
        help="iterates through 'heavy' intermediate")
    sub.set_defaults(func='run_transform')
    sub.add_argument("json_file",
        help="intermediate globs as JSON-lines",
        nargs='?', default=sys.stdin, type=argparse.FileType('r'))

    args = parser.parse_args()
    if not args.__dict__.get("func"):
        print("tell me what to do! (try --help)")
        sys.exit(-1)

    if args.func == 'run_transform':
        run_transform(infile=args.json_file)
    else:
        raise NotImplementedError(args.func)

if __name__=="__main__":
    main()
