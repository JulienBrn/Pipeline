from __future__ import annotations
from typing import Dict, Tuple, NamedTuple, List, Callable, Any, Protocol, Container, Mapping
from dataclasses import dataclass
import pandas as pd
from pathlib import Path
import inspect, functools, logging
import collections, collections.abc, types

logger=logging.getLogger(__name__)

@dataclass
class CoordComputer:
    coords: set[str]
    dependencies: set[str]
    compute: Callable[[DatabaseInstance, pd.DataFrame], pd.DataFrame]

    @staticmethod
    def from_function(coords=None, dependencies=None, vectorized=False, adapt_return=True, database_arg=None):
        def decorate(f):
            nonlocal coords, dependencies, vectorized, adapt_return
            if coords is None:
                coords=set([f.__name__])
            if dependencies is None:
                if vectorized:
                    raise Exception("Unable to determine dependencies")
                else:
                    dependencies = set(inspect.signature(f).parameters.keys())
            if database_arg is None:
                f2 = functools.wraps(f)(lambda db, *args, **kwargs: f(*args, **kwargs))
            else:
                f2=f
                dependencies.remove(database_arg)
            if adapt_return:
                @functools.wraps(f2)
                def new_f(*args, **kwargs):
                    res = f2(*args, **kwargs)
                    if not isinstance(res, pd.DataFrame):
                        res = pd.DataFrame(res, columns=list(coords))
                    if not set(coords).issubset(set(res.columns)):
                        raise Exception(f"Problem in coord definition")
                    return res
            else:
                new_f = f2
            if not vectorized:
                def compute(db, df: pd.DataFrame):
                    tmp_dfs = []
                    for _, row in df.iterrows():
                        d = row.to_dict()
                        try:
                            tmp = new_f(db, **d)
                        except Exception as e:
                            e.add_note(f'Parameters are {d}')
                            raise e
                        for k,v in d.items():
                            tmp[k] = v
                        tmp_dfs.append(tmp)
                    return pd.concat(tmp_dfs)
            else:
                compute= new_f
            f._databaseobject = CoordComputer(coords=set(coords), dependencies=set(dependencies), compute=compute)
            return f
        return decorate


class ActionProtocol(Protocol):
    def __call__(self, d: DatabaseInstance, out_location: Path | Any, coords: Dict[str, Any]) -> None: ...

@dataclass
class Data:
    name: str
    dependencies: set[str]
    get_location: Callable[[Mapping[str, Any]], Any | None]
    actions: Mapping[str, ActionProtocol]

    @staticmethod
    def from_class(actions=None):
        def decorate(cls):
            nonlocal actions
            if not hasattr(cls, "location"):
                raise Exception("No location function")
            loc_func = getattr(cls, "location")
            loc_func_args = set(inspect.signature(loc_func).parameters.keys())
            name = getattr(cls, "name") if hasattr(cls, "name") else cls.__name__ 
            def loc_func_debug(*args, **kwargs):
                # print(f'Calling {getattr(cls, "location")}')
                try:
                    r = loc_func(*args, **kwargs)
                except:
                    import traceback
                    traceback.print_exc()
                    exit()
                # print(r)
                return r
            if actions is None:
                actions = [meth  for meth in dir(cls) if isinstance(inspect.getattr_static(cls, meth), staticmethod) and meth!="location"]
            
            cls._databaseobject = Data(name=name, 
                                       dependencies=loc_func_args, 
                                       get_location=lambda d: loc_func_debug(**{k: v for k,v in d.items() if k in loc_func_args}), 
                                       actions = {a:getattr(cls, a) for a in actions}
            )
            return cls
        return decorate

class Database:
    coord_computers = List[CoordComputer]
    data: Dict[str, Data]
    name: str

    def __init__(self, name):
        self.name=name
        self.coord_computers = []
        self.data = {}
        # self.declare(CoordComputer(coords={"pipeline"}, dependencies={}, compute = lambda db, df: df.assign(dict(pipeline=self.name))))

    def declare(self, o):
        if isinstance(o, CoordComputer):
            self.coord_computers.append(o)
        elif isinstance(o, Data):
            if o.name in self.data:
                raise Exception(f"Data {o.name} already exists")
            self.data[o.name] = o
        else:
            raise Exception(f'Cannot declare object of type {type(o)}')

    def register(self, f):
        self.declare(f._databaseobject)
        return f

    def initialize(self):
        inst = DatabaseInstance(self)
        return inst

    @staticmethod
    def join(*dbs: Database, new_name=None):
        if new_name is None:
            new_name = f'Joined({", ".join([db.name for db in dbs])})'
        res = Database(new_name)
        for db in dbs:
            for cc in db.coord_computers:
                res.declare(cc)
            for d in db.data.values():
                res.declare(d)
        return res

    
        

class DatabaseInstance:
    db: Database
    coords: Dict[str, CoordComputer]

    def __init__(self, db: Database):
        self.db = db
        self.coords = {}
        self.continue_on_error=False
        for cc in db.coord_computers:
            for c in cc.coords:
                if c in self.coords:
                    raise Exception(f"Coordinate {c} has two computers")
                else:
                    self.coords[c] = cc
    

    def _get_dependencies(self, names: set[str]):
        res = set()
        for n in names:
            res = res.union(set([n]), self._get_dependencies(self.coords[n].dependencies))
        return res
    
    @staticmethod
    def _filter(df: pd.DataFrame, selection_dict):
        for s, v in selection_dict.items():
            if s in df.columns:
                if isinstance(v, tuple):
                    df = df.loc[df[s].isin(v)]
                elif isinstance(v, slice):
                    if v.step:
                        Exception("step not handled")
                    start_cond = df[s] >= v.start if v.start else True
                    end_cond = df[s] < v.stop if v.stop else True
                    df = df.loc[start_cond & end_cond]
                elif isinstance(v, Callable):
                    df = df.loc[df[s].apply(v)]
                else:
                    df = df.loc[df[s] ==v]
        return df
    

    def get_coords(self, names, selection_dict = {},  single=False, **selection_kwargs):
        from frozendict import frozendict
        selection_dict = selection_dict | selection_kwargs
        if isinstance(names, str):
            names = [names]
        res = (
                (self._get_coords_impl)(frozenset(names), frozendict(selection_dict)).copy(deep=True)
        )
        if not single:
            return res
        else :
            return self._extract_single(res, names[0] if len(names) ==1 else names)
            
    @functools.cache
    def _get_coords_impl(self, names: set[str], selection_dict):
        all_names = self._get_dependencies(names)
        
        df = pd.DataFrame([[]])
        # print("ENTER", names, selection_dict)
        while not all_names.issubset(set(df.columns)):
            for cc in self.db.coord_computers:
                if cc.coords.intersection(all_names) != set() and cc.dependencies.issubset(set(df.columns)) and not cc.coords.issubset(set(df.columns)):
                    # print(cc)
                    # print(df)
                    # tmp_dfs = []
                    dep_list = list(cc.dependencies)
                    param_df = df[dep_list].drop_duplicates()
                    # print("NEWWWWWW")
                    # print(param_df)
                    try:
                        computed_df = cc.compute(self, param_df)
                        # print(computed_df)
                        tmp_df = self._filter(computed_df, selection_dict)
                    except Exception as e:
                        e.add_note(f'While computing {cc.coords}')
                        raise e
                    # print(tmp_df)
                    # print(selection_dict)
                    df = pd.merge(df, tmp_df, on=list(cc.dependencies), how="inner") if len(dep_list) > 0 else pd.merge(df, tmp_df, how="cross")             
                    # print(df)
                    # print(f'columns={set(df.columns)}')
        cols_to_remove=[]
        def keep_unique_columns(df):
            cols = []
            for col in df.columns:
                if (df[col] == df[col].iat[0]).all() and ~pd.isna(df[col].iat[0]):
                    cols.append(col)
                else:
                    cols_to_remove.append(col)
            return df[cols].head(1)
        
        if len(names) > 0:
            res = df.groupby(list(names), group_keys=False).apply(keep_unique_columns).reset_index(drop=True)
            res = res[[col for col in res.columns if not col in cols_to_remove]]
        else:
            raise Exception("Strange")
            res = df
        # print("done")
        # print(names)
        # print(selection_dict)
        # print(res)
        # input()
        return res
    
    def get_locations(self, name, selection_dict={}, **selection_kwargs) -> pd.DataFrame:
        coords = self.get_coords(self.db.data[name].dependencies, selection_dict, **selection_kwargs)
        coords["location"] = coords.apply(lambda row: self.db.data[name].get_location(row.to_dict()), axis=1)
        return coords.loc[~pd.isna(coords["location"])]
    
    @staticmethod
    def _extract_single(df: pd.DataFrame, col: str):
        if len(df.index) != 1:
            raise Exception(f"Problem. Expected a single line, got\n{df}")
        else:
            return df.iloc[0][col]

    def get_single_location(self, name, selection_dict={}, **selection_kwargs) -> Path:
        return DatabaseInstance._extract_unique(self.get_locations(name, selection_dict, **selection_kwargs), "location")
        
        
    def run_action(self, action, target, selection_dict={}, single=False, **selection_kwargs) -> pd.DataFrame | Any:
        locs = self.get_locations(target, selection_dict, **selection_kwargs)
        if locs["location"].duplicated().any():
            raise Exception(f'Duplication problem\n{locs[locs["location"].isin(locs["location"][locs["location"].duplicated()])]}')
        if action != "location":
            import tqdm.auto as tqdm
            results = []
            errors = []
            progress = tqdm.tqdm(desc=f"{action} {target}", disable=len(locs.index) < 3, total = len(locs.index))
            for _, row in locs.iterrows():
                if self.continue_on_error:
                    progress.set_postfix(n_errors=len(errors), curr=row["location"])
                else:
                    progress.set_postfix(curr=row["location"])
                try:
                    results.append(self.db.data[target].actions[action](self, row["location"], row.drop("location").to_dict()))
                except Exception as e:
                    e.add_note(f'During {action} for {target}({row["location"]}, {row.drop("location").to_dict()})')
                    if not self.continue_on_error:
                        raise
                    else:
                        errors.append(e)
                progress.update(1)
            if len(errors) > 0:
                raise ExceptionGroup(f'During {action} for {target}', errors)
            import numpy as np
            if len(locs.index) == 1:
                locs[action] = pd.Series(dtype="object")
                locs[action].iat[0] = results[0]
            else:
                locs[action] = results
            
        if single:
            return DatabaseInstance._extract_single(locs, action)
        else:
            return locs

    def compute(self, name,  selection_dict={}, **selection_kwargs) -> pd.DataFrame:
        return self.run_action("compute", name, selection_dict, **selection_kwargs, single=False)
    
    def compute_single(self, name,  selection_dict={}, **selection_kwargs) -> Path:
        return self.run_action("compute", name, selection_dict, **selection_kwargs, single=True)


    def __str__(self):
        tmp_df = pd.DataFrame()
        tmp_df["key"] = list(self.db.data.keys())
        tmp_df["dependency"] = tmp_df["key"].apply(lambda k: ", ".join(tuple(self.db.data[k].dependencies)))
        tmp_df["actions"] = tmp_df["key"].apply(lambda k:  ", ".join(tuple(self.db.data[k].actions.keys())))

        coord_df = pd.DataFrame()
        coord_df["key"] = [", ".join(cc.coords) for cc in self.db.coord_computers]
        coord_df["dependency"] = [", ".join(cc.dependencies) for cc in self.db.coord_computers]

        coord_df = coord_df.replace("", "None")
        tmp_df = tmp_df.replace("", "None")
        return (
            f'Database {self.db.name}\n'+
            f'\tCoordinates:\n\t\t'+coord_df.set_index("key").to_string().replace("\n", "\n\t\t") +
            f'\n\tItems:\n\t\t' + 
            tmp_df.set_index("key").to_string().replace("\n", "\n\t\t")

            
        )
    

def default_saver(path: Path, o):
    import pickle
    if path.suffix == ".tsv" and isinstance(o, (pd.DataFrame, pd.Series)):
        if not type(o.index) == pd.RangeIndex:
            raise Exception("Automatic tsv writing does not store index information. Please reset your index... (either to drop it or to name it as a column) or use a dedicated saver")
        o.to_csv(path, sep="\t", index=False)
    else:
        with path.open("wb") as f:
            pickle.dump(o, f)

def safe_save(saver):
    import shutil
    def new_saver(path: Path, o):
        suffix = path.suffix
        tmp_path = path.parent / (path.stem + ".tmp" + suffix)
        tmp_path.parent.mkdir(exist_ok=True, parents=True)
        saver(tmp_path, o)
        shutil.move(tmp_path, path)
    return new_saver

def open_and_save(saver, mode):
    def new_saver(path, o):
        with path.open(mode) as f:
            saver(f, o)
    return new_saver

def cache(saver=default_saver, open=None, force_recompute=False):
    def decorator(f):
        @functools.wraps(f)
        def new_f(db, out_location, *args, **kwargs):
            if out_location.exists() and not force_recompute:
                return out_location
            res = f(db, out_location, *args, **kwargs)
            if res is None:
                return out_location
            if open is None:
                msaver= saver
            else:
                msaver = open_and_save(saver, open)
            safe_save(msaver)(out_location, res)
            return out_location
        return new_f
    return decorator

def singleglob(p: Path, *patterns, error_string='Found {n} candidates for pattern {patterns} in folder {p}', only_ok=False):
    all = [path for pat in patterns for path in p.glob(pat)]
    if only_ok:
        return len(all)==1
    if len(all) >1:
        raise FileNotFoundError(error_string.format(p=p, n=len(all), patterns=patterns))
    if len(all) ==0:
        raise FileNotFoundError(error_string.format(p=p, n=len(all), patterns=patterns))
    return all[0]

def get_fs(a):
    import numpy as np
    period = np.mean(a[1:] - a[:-1])
    new_arr = np.arange(a.size)*period+a[0]
    if (np.abs(a-new_arr) > 0.01*period).any():
        raise Exception("Array not regular")
    return 1/period

def precompute(name, action="compute"):
    def decorate(f):
        @staticmethod
        def new_f(db, out_location, selection):
            db.run_action(action, name, selection, single=True)
            return f(db, out_location, selection)
        return new_f
    return decorate

def tsvload(db, out_location, selection):
    return pd.read_csv(out_location, sep="\t", index_col=None)
