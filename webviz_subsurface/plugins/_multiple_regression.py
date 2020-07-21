from uuid import uuid4
from pathlib import Path

import warnings
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash_table import DataTable
import dash_bootstrap_components as dbc
from dash.dependencies import Input, Output, State
import dash_html_components as html
import dash_core_components as dcc
from dash_table.Format import Format, Scheme
import webviz_core_components as wcc
from webviz_config.webviz_store import webvizstore
from webviz_config.common_cache import CACHE
from webviz_config import WebvizPluginABC
from webviz_config.utils import calculate_slider_step
import statsmodels.api as sm
from sklearn.preprocessing import PolynomialFeatures
from itertools import combinations
import plotly.express as px
import numpy.linalg as la
from .._datainput.fmu_input import load_parameters, load_csv
from .._utils.ensemble_handling import filter_and_sum_responses
import time


class MultipleRegression(WebvizPluginABC):
    """### Best fit using forward stepwise regression

This plugin shows a multiple regression of numerical parameters and a response.

The model uses a modified forward selection algorithm to choose the most relevant parameters,

Input can be given either as:

- Aggregated csv files for parameters and responses,
- An ensemble name defined in shared_settings and a local csv file for responses
stored per realizations.

**Note**: Non-numerical (string-based) input parameters and responses are removed.

**Note**: The response csv file will be aggregated per realization.

**Note**: Regression models break down when there are duplicate or highly correlated parameters,
            please make sure to properly filter your inputs as the model will give a response, but it will be wrong.
Arguments:

* `parameter_csv`: Aggregated csvfile for input parameters with 'REAL' and 'ENSEMBLE' columns.
* `response_csv`: Aggregated csvfile for response with 'REAL' and 'ENSEMBLE' columns.
* `ensembles`: Which ensembles in `shared_settings` to visualize. If neither response_csv or
            response_file is defined, the definition of ensembles implies that you want to
            use simulation timeseries data directly from UNSMRY data. This also implies that
            the date will be used as a response filter of type `single`.
* `response_file`: Local (per realization) csv file for response parameters.
* `response_filters`: Optional dictionary of responses (columns in csv file) that can be used
as row filtering before aggregation. (See below for filter types).
* `response_ignore`: Response (columns in csv) to ignore (cannot use with response_include).
* `response_include`: Response (columns in csv) to include (cannot use with response_ignore).
* `column_keys`: Simulation vectors to use as responses read directly from UNSMRY-files in the
                defined ensembles using fmu-ensemble (cannot use with response_file,
                response_csv or parameters_csv).
* `sampling`: Sampling frequency if using fmu-ensemble to import simulation time series data.
            (Only relevant if neither response_csv or response_file is defined). Default monthly
* `aggregation`: How to aggregate responses per realization. Either `sum` or `mean`.
* `corr_method`: Correlation algorithm. Either `pearson` or `spearman`.

The types of response_filters are:
```
- `single`: Dropdown with single selection.
- `multi`: Dropdown with multiple selection.
- `range`: Slider with range selection.
```
"""

    # pylint:disable=too-many-arguments
    def __init__(
        self,
        app,
        parameter_csv: Path = None,
        response_csv: Path = None,
        ensembles: list = None,
        response_file: str = None,
        response_filters: dict = None,
        response_ignore: list = None,
        response_include: list = None,
        column_keys: list = None,
        sampling: str = "monthly",
        aggregation: str = "sum",
        parameter_ignore: list = None,
    ):

        super().__init__()

        self.parameter_csv = parameter_csv if parameter_csv else None
        self.response_csv = response_csv if response_csv else None
        self.response_file = response_file if response_file else None
        self.response_filters = response_filters if response_filters else {}
        self.response_ignore = response_ignore if response_ignore else None
        self.parameter_ignore = parameter_ignore if parameter_ignore else None
        self.column_keys = column_keys
        self.time_index = sampling
        self.aggregation = aggregation

        if response_ignore and response_include:
            raise ValueError(
                'Incorrent argument. either provide "response_include", '
                '"response_ignore" or neither'
            )
        if parameter_csv and response_csv:
            if ensembles or response_file:
                raise ValueError(
                    'Incorrect arguments. Either provide "csv files" or '
                    '"ensembles and response_file".'
                )
            #For csv files
            #self.parameterdf = read_csv(self.parameter_csv)
            #self.responsedf = read_csv(self.response_csv)

            #For parquet files
            self.parameterdf = pd.read_parquet(self.parameter_csv)
            self.responsedf = pd.read_parquet(self.response_csv)

        elif ensembles and response_file:
            self.ens_paths = {
                ens: app.webviz_settings["shared_settings"]["scratch_ensembles"][ens]
                for ens in ensembles
            }
            self.parameterdf = load_parameters(
                ensemble_paths=self.ens_paths, ensemble_set_name="EnsembleSet"
            )
            self.responsedf = load_csv(
                ensemble_paths=self.ens_paths,
                csv_file=response_file,
                ensemble_set_name="EnsembleSet",
            )
        else:
            raise ValueError(
                'Incorrect arguments.\
                 Either provide "csv files" or "ensembles and response_file".'
            )
        self.check_runs()
        self.check_response_filters()
        if response_ignore:
            self.responsedf.drop(
                response_ignore,
                errors="ignore", axis=1, inplace=True)
        if response_include:
            self.responsedf.drop(
                self.responsedf.columns.difference(
                    [
                        "REAL",
                        "ENSEMBLE",
                        *response_include,
                        *list(response_filters.keys()),
                    ]
                ),
                errors="ignore",
                axis=1,
                inplace=True,
            )
        if parameter_ignore:
            self.parameterdf.drop(parameter_ignore, axis=1, inplace=True)

        self.plotly_theme = app.webviz_settings["theme"].plotly_theme
        self.uid = uuid4()
        self.set_callbacks(app)

    def ids(self, element):
        """Generate unique id for dom element"""
        return f"{element}-id-{self.uid}"

    @property
    def tour_steps(self):
        steps = [
            {
                "id": self.ids("layout"),
                "content": (
                    "Dashboard displaying the results of a multiple "
                    "regression of input parameters and a chosen response."
                )
            },
            {
                "id": self.ids("table"),
                "content": (
                    "A table showing the results for the best combination of "
                    "parameters for a chosen response."
                )
            },
            {
                "id": self.ids("p-values-plot"),
                "content": (
                    "A plot showing the p-values for the parameters from the table ranked from most significant "
                    "to least significant.  Red indicates "
                    "that the p-value is significant, gray indicates that the p-value "
                    "is not significant."
                )
            },
            {
                "id": self.ids("coefficient-plot"),
                "content": (
                    "A plot showing the sign of parameters' coefficient values by arrows pointing up and/or down, "
                    "illustrating a positive and/or negative coefficient respectively. " #Tung setning?
                    "An arrow is red if the corresponding p-value is significant, that is, a p-value below 0.05. "
                    "Arrows corresponding to p-values above this level of significance, are shown in gray."
                )
            },
            {"id": self.ids("ensemble"), "content": ("Select the active ensemble."), },
            {"id": self.ids("responses"), "content": ("Select the active response."), },
            {"id": self.ids("max-params"), "content": ("Select the maximum number of parameters to be included in the regression."), },
            {"id": self.ids("force-in"), "content": ("Choose parameters to include in the regression."), },
            {"id": self.ids("interaction"), "content": ("Toggle interaction on/off between the parameters."), },
            {"id": self.ids("submit-btn"), "content": ("Click SUBMIT to update the table and the plots."), },
        ]
        return steps

    @property
    def responses(self):
        """Returns valid responses. Filters out non numerical columns,
        and filterable columns. Replaces : and , with _ to make it work with the model"""
        responses = list(
            self.responsedf.drop(["ENSEMBLE", "REAL"], axis=1)
            .apply(pd.to_numeric, errors="coerce")
            .dropna(how="all", axis="columns")
            .columns
        )
        return [p for p in responses if p not in self.response_filters.keys()]

    @property
    def parameters(self):
        """Returns numerical input parameters"""
        parameters = list(
            self.parameterdf.drop(["ENSEMBLE", "REAL"], axis=1)
            .apply(pd.to_numeric, errors="coerce")
            .dropna(how="all", axis="columns")
            .columns
        )
        return parameters

    @property
    def ensembles(self):
        """Returns list of ensembles"""
        return list(self.parameterdf["ENSEMBLE"].unique())

    def check_runs(self):
        """Check that input parameters and response files have
        the same number of runs"""
        for col in ["ENSEMBLE", "REAL"]:
            if sorted(list(self.parameterdf[col].unique())) != sorted(
                list(self.responsedf[col].unique())
            ):
                raise ValueError("Parameter and response files have different runs")

    def check_response_filters(self):
        """'Check that provided response filters are valid"""
        if self.response_filters:
            for col_name, col_type in self.response_filters.items():
                if col_name not in self.responsedf.columns:
                    raise ValueError(f"{col_name} is not in response file")
                if col_type not in ["single", "multi", "range"]:
                    raise ValueError(
                        f"Filter type {col_type} for {col_name} is not valid."
                    )


    @property
    def filter_layout(self):
        """Layout to display selectors for response filters"""
        children = []
        for col_name, col_type in self.response_filters.items():
            domid = self.ids(f"filter-{col_name}")
            values = list(self.responsedf[col_name].unique())
            if col_type == "multi":
                selector = wcc.Select(
                    id=domid,
                    options=[{"label": val, "value": val} for val in values],
                    value=values,
                    multi=True,
                    size=min(20, len(values)),
                )
            elif col_type == "single":
                selector = dcc.Dropdown(
                    id=domid,
                    options=[{"label": val, "value": val} for val in values],
                    value=values[0],
                    multi=False,
                    clearable=False,
                )
            else:
                return children
            children.append(html.Div(children=[html.Label(col_name), selector,]))
        return children

    @property
    def control_layout(self):
        """Layout to select e.g. iteration and response"""
        return [
            html.Div(
                style={
                    "display": "grid",
                    "gridTemplateRows": "1fr 1fr",
                },
                children=[
                    html.Label("Press 'SUBMIT' to activate changes"),
                    html.Button(
                        id=self.ids("submit-btn"), 
                        children="Submit",
                    )
                ]
            ),
            html.Div(
                [
                    html.Label("Ensemble"),
                    dcc.Dropdown(
                        id=self.ids("ensemble"),
                        options=[
                            {"label": ens, "value": ens} for ens in self.ensembles
                        ],
                        clearable=False,
                        value=self.ensembles[0],
                    ),
                ]
            ),
            html.Div(
                [
                    html.Label("Response"),
                    dcc.Dropdown(
                        id=self.ids("responses"),
                        options=[
                            {"label": ens, "value": ens} for ens in self.responses
                        ],
                        clearable=False,
                        value=self.responses[0],
                    ),
                ]
            ),
            html.Div(
                style={"flex": 1},
                children=self.filter_layout
            ),
            html.Div(
                [
                    html.Label("Interaction"),
                    dcc.Slider(
                        id=self.ids("interaction"),
                        min=0,
                        max=2, 
                        step=None,
                        marks={
                            0: "Off",
                            1: "2 levels",
                            2: "3 levels"
                        },
                        value=0
                    )
                ]
            ),
            html.Div(
                [
                    html.Label("Max number of parameters"),
                    dcc.Dropdown(
                        id=self.ids("max-params"),
                        options=[
                            {"label": val, "value": val} for val in range(1, min(20, len(self.parameterdf.columns)))
                        ],
                        clearable=False,
                        value=3,
                    ),
                ]
            ),
            html.Div(
                [
                   dcc.RadioItems(
                       id=self.ids("exclude_include"),
                       options=[
                           {"label": "Exclude parameters", "value": "exc"},
                           {"label": "Only include paramters", "value": "inc"}
                       ],
                       value="exc",
                       labelStyle={'display': 'inline-block'}
                   )
               ]
            ),
             html.Div(
                [
                    dcc.Dropdown(
                        id=self.ids("parameter-list"),
                        options=[
                            {"label": ens, "value": ens} for ens in self.parameters
                        ],
                        clearable=True,
                        multi=True,
                        placeholder="",
                        value=[],
                    ),
                ]
            ),
            html.Div(
                [
                    html.Label("Force in", style={'display': 'inline-block', 'margin-right': '10px'}),
                    html.Abbr("\u24D8", title="Hello, I am hover-enabled helpful information"),
                    dcc.Dropdown(
                        id=self.ids("force-in"),
                        clearable=True,
                        multi=True,
                        placeholder='Describe force-in here',
                        value=[],

                    )
                ]
            ),
        ]

    @property
    def layout(self):
        """Main layout"""
        return wcc.FlexBox(
            id=self.ids("layout"),
            children=[
                html.Div(
                    style={"flex": 3},
                    children=[
                        html.Div(
                            id=self.ids("table_title"),
                            style={"textAlign": "center"}
                        ),
                        DataTable(
                            id=self.ids("table"),
                            sort_action="native",
                            filter_action="native",
                            page_action="native",
                            page_size=10,
                            style_cell={"fontSize": 14}
                        ),
                        html.Div(
                            style={'flex': 3},
                            children=[
                                wcc.Graph(id=self.ids('p-values-plot')),
                            ]
                        ),
                        html.Div(
                            style={'flex': 3},
                            children=[
                                wcc.Graph(id=self.ids('coefficient-plot')),
                            ]
                        ),
                    ],
                ),
                html.Div(
                    style={"flex": 1},
                    children=self.control_layout
                    #if self.response_filters
                    #else [],
                )
            ]
        )

    @property
    def model_input_callbacks(self):
        """List of inputs for multiple regression callback"""
        callbacks = [
            State(self.ids("exclude_include"), "value"),
            State(self.ids("parameter-list"), "value"),
            State(self.ids("ensemble"), "value"),
            State(self.ids("responses"), "value"),
            State(self.ids("force-in"), "value"),
            State(self.ids("interaction"), "value"),
            State(self.ids("max-params"), "value"),
        ]
        if self.response_filters:
            for col_name in self.response_filters:
                callbacks.append(State(self.ids(f"filter-{col_name}"), "value"))
        return callbacks
    @property
    def detect_changes_callbacks(self):
        callbacks = [
            Input(self.ids("exclude_include"), "value"),
            Input(self.ids("parameter-list"), "value"),
            Input(self.ids("ensemble"), "value"),
            Input(self.ids("responses"), "value"),
            Input(self.ids("force-out"), "value"),
            Input(self.ids("force-in"), "value"),
            Input(self.ids("interaction"), "value"),
            Input(self.ids("max-params"), "value"),
        ]
        if self.response_filters:
            for col_name in self.response_filters:
                callbacks.append(Input(self.ids(f"filter-{col_name}"), "value"))
        return callbacks


    def make_response_filters(self, filters):
        """Returns a list of active response filters"""
        filteroptions = []
        if filters:
            for i, (col_name, col_type) in enumerate(self.response_filters.items()):
                filteroptions.append(
                    {"name": col_name, "type": col_type, "values": filters[i]}
                )
        return filteroptions

    def set_callbacks(self, app):
        """Set callbacks for placeholder text for exc/inc dropdown"""
        @app.callback(
                Output(self.ids("parameter-list"), "placeholder"),
                [Input(self.ids("exclude_include"), "value")]
        )
        def update_placeholder(exc_inc):
            if exc_inc == 'exc':
                return "Smart exclude text goes here"
            elif exc_inc == 'inc':
                return 'Smart include text goes here'

        """Set callbacks for interaction between exclude/include param and force-in"""
        @app.callback(
                Output(self.ids("force-in"), "options"),
            [
                Input(self.ids("parameter-list"), "value"),
                Input(self.ids("exclude_include"), "value")
            ]
        )
        def update_force_in(parameter_list, exc_inc):
            """Returns a dictionary with options for force in"""
            #If exclusive and parameter_list empty -> all param avail. for force-in
            #If inclusive and parameter_list empty -> no param avail.
            if exc_inc == "exc":
                df = self.parameterdf.drop(columns=["ENSEMBLE", "REAL"] + parameter_list)
            elif exc_inc == "inc":
                df = self.parameterdf[parameter_list] if parameter_list else []

            fi_lst = list(df)
            return [{"label": fi, "value": fi} for fi in fi_lst]

        @app.callback(
            Output(self.ids("submit-btn"),"children"),
            self.detect_changes_callbacks
        )
        def update_submit_on_change(*args):
            return "Press to update model"
        """Set callbacks for the table, p-values plot, and arrow plot"""
        @app.callback(
            [
                Output(self.ids("table"), "data"),
                Output(self.ids("table"), "columns"),
                Output(self.ids("table_title"), "children"),
                Output(self.ids("p-values-plot"), "figure"),
                Output(self.ids("coefficient-plot"), "figure")
            ],
            [
                Input(self.ids("submit-btn"), "n_clicks")
            ],
            self.model_input_callbacks,
        )
        def _update_visualizations(n_clicks, exc_inc, parameter_list, ensemble, response, force_in, interaction, max_vars, *filters):
            """Callback to update the model for multiple regression

            1. Filters and aggregates response dataframe per realization
            2. Filters parameters dataframe on selected ensemble
            3. Merge parameter and response dataframe
            4. Fit model using forward stepwise regression, with or without interactions
            5. Generate table and plots
            """
            filteroptions = self.make_response_filters(filters)
            responsedf = filter_and_sum_responses(
                self.responsedf,
                ensemble,
                response,
                filteroptions=filteroptions,
                aggregation=self.aggregation,
            )
            if exc_inc == "exc":
                parameterdf = self.parameterdf.drop(parameter_list, axis=1)
            elif exc_inc == "inc":
                parameterdf = self.parameterdf[["ENSEMBLE", "REAL"] + parameter_list]

            parameterdf = parameterdf.loc[self.parameterdf["ENSEMBLE"] == ensemble]
            df = pd.merge(responsedf, parameterdf, on=["REAL"]).drop(columns=["REAL", "ENSEMBLE"])

            #If no selected parameters
            if exc_inc == "inc" and not parameter_list:
                return(
                    [{"e": ""}],
                    [{"name": "", "id": "e"}],
                    "Please select parameters to be included in the model",
                    {
                    "layout": {
                        "title": "<b>Please select parameters to be included in the model</b><br>"
                        }
                    },
                    {
                    "layout": {
                        "title": "<b>Please select parameters to be included in the model</b><br>"
                        }
                    },
                )
                
            else:
                # Get results from the model
                result = gen_model(df, response, force_in =force_in, max_vars=max_vars, interaction_degree=interaction)
                if not result:
                        return(
                    [{"e": ""}],
                    [{"name": "", "id": "e"}],
                    "Cannot calculate fit for given selection. Select a different response or filter setting",
                    {
                    "layout": {
                        "title": "<b>Cannot calculate fit for given selection</b><br>"
                        "Select a different response or filter setting."
                        }
                    },
                    {
                        "layout": {
                            "title": "<b>Cannot calculate fit for given selection</b><br>"
                            "Select a different response or filter setting."
                        }
                    },
                    )  
                # Generate table
                table = result.model.fit().summary2().tables[1].drop("Intercept")
                table.drop(["Std.Err.", "t", "[0.025","0.975]"], axis=1, inplace=True)
                table.index.name = "Parameter"
                table.reset_index(inplace=True)
                columns = [{"name": i, "id": i, 'type': 'numeric', "format": Format(precision=4)} for i in table.columns]
                data = table.to_dict("rows")

                # Get p-values for plot
                p_sorted = result.pvalues.sort_values().drop("Intercept")

                # Get coefficients for plot
                coeff_sorted = result.params.sort_values(ascending=False).drop("Intercept")

                return(
                    # Generate table
                    data,
                    columns,
                    f"Multiple regression with {response} as response",

                    # Generate p-values plot
                    make_p_values_plot(p_sorted, self.plotly_theme),

                    # Generate coefficient plot
                    make_arrow_plot(coeff_sorted, p_sorted, self.plotly_theme)
                )
            
    def add_webvizstore(self):
        if self.parameter_csv and self.response_csv:
            return [
                (read_csv, [{"csv_file": self.parameter_csv,}],),
                (read_csv, [{"csv_file": self.response_csv,}],),
            ]
        return [
            (
                load_parameters,
                [
                    {
                        "ensemble_paths": self.ens_paths,
                        "ensemble_set_name": "EnsembleSet",
                    }
                ],
            ),
            (
                load_csv,
                [
                    {
                        "ensemble_paths": self.ens_paths,
                        "csv_file": self.response_file,
                        "ensemble_set_name": "EnsembleSet",
                    }
                ],
            ),
        ]



@CACHE.memoize(timeout=CACHE.TIMEOUT)
def gen_model(
        df: pd.DataFrame,
        response: str,
        max_vars: int=9,
        force_in: list=[],
        interaction_degree: bool=False
    ):
    """wrapper for modelselection algorithm."""
    if interaction_degree:
        df = _gen_interaction_df(df, response, interaction_degree + 1)
        model = forward_selected(
            data=df,
            response=response,
            force_in=force_in,
            maxvars=max_vars
            )
    else:
        model = forward_selected(
            data=df,
            response=response,
            force_in=force_in,
            maxvars=max_vars
        ) 
    return model

@CACHE.memoize(timeout=CACHE.TIMEOUT)
def _gen_interaction_df(
    df: pd.DataFrame,
    response: str,
    degree: int=4):
    newdf = df.copy()

    name_combinations = []
    for i in range(1, degree+1):
        name_combinations += ["*".join(combination) for combination in combinations(newdf.drop(columns=response).columns, i)]
    for name in name_combinations:
        if name.split("*"):
            newdf[name] = newdf.filter(items=name.split("*")).product(axis=1)
    return newdf




def forward_selected(data: pd.DataFrame,
                     response: str, 
                     force_in: list=[], 
                     maxvars: int=5):
    """ Forward model selection algorithm

        Return statsmodels RegressionResults object
        the algortihm is a modified standard forward selection algorithm. 
        The selection criterion chosen is adjusted R squared.
        See this link for more info on algorithm: 
        https://en.wikipedia.org/wiki/Stepwise_regression
     
        step by step of the algorithm:
        - initialize values
        - while there are parameters left and the last model was the best model yet and the parameter limit isnt reached
        - for every parameter not chosen yet.
            1. if it is an interaction parameter add the base features to the model
            2. create model matrix
     """

    # Initialize values for use in algorithm
    # y is the response SST in the total sum of squares
    y = data[response].to_numpy(dtype="float64")
    n = len(y)
    y_mean = np.mean(y)
    SST = np.sum((y-y_mean) ** 2)
    remaining = set(data.columns).difference(set(force_in+[response]))
    selected = force_in
    current_score, best_new_score = 0.0, 0.0

    while remaining and current_score == best_new_score and len(selected) < maxvars:
        # The alogrithm works as follows
        # check if remaining is empty, if the last round was an improvement, and if we reached the variable limit
         
        scores_with_candidates = []
        for candidate in remaining:
           
            # for every candidate in the remaining data, if it is an interaction term add the underlying features
            # create a model matrix with all the parameters previously choosen and the candidate with eventual base cases.
            if "*" in candidate:
                current_model = selected.copy() + [candidate] + list(set(candidate.split("*")).difference(set(selected)))
            else:
                current_model = selected.copy()+[candidate]
            X = data.filter(items=current_model).to_numpy(dtype="float64")
            p = X.shape[1]  
            X = np.append(X, np.ones((len(y), 1)), axis=1)
            
            # Fit model 
            try: 
                beta = la.inv(X.T @ X) @ X.T @ y
            except la.LinAlgError:
                # this clause lets us skip singluar and other non-valid model matricies.
                continue

            if n - p - 1 < 1: 
                
                # the exit condition means adding this parameter would add more parameters than observations, 
                # this causes infinite variance in the model so we return the current best model
                
                model_df = data.filter(items=selected)
                model_df["Intercept"] =  np.ones((len(y), 1))
                model_df["response"] = y
                
                return _model_warnings(model_df)

            # adjusted R squared is our chosen model selection criterion.
            f_vec = beta @ X.T
            SS_RES = np.sum((f_vec-y_mean) ** 2)
            
            R_2_adj = 1-(1 - (SS_RES / SST))*((n-1)/(n-p-1))
            scores_with_candidates.append((R_2_adj, candidate))

        # if the best parameter is interactive, add all base features
        scores_with_candidates.sort(key=lambda x: x[0])
        best_new_score, best_candidate = scores_with_candidates.pop()
        if current_score < best_new_score:
            if "*" in best_candidate:
                for base_feature in best_candidate.split("*"):
                    if base_feature in remaining:
                        remaining.remove(base_feature)
                    if base_feature not in selected:
                        selected.append(base_feature)
            
            remaining.remove(best_candidate)
            selected.append(best_candidate)
            current_score = best_new_score
    
    # finally fit a statsmodel from the selected parameters
    model_df = data.filter(items=selected)
    model_df["Intercept"] =  np.ones((len(y), 1))
    model_df["response"]=y
    return _model_warnings(model_df)

def _model_warnings(design_matrix: pd.DataFrame):
    with warnings.catch_warnings():
        # handle warnings so the graphics indicate explicity that the model failed for the current input. 
        warnings.filterwarnings('error', category=RuntimeWarning)
        warnings.filterwarnings('ignore', category=UserWarning)
        try:
            model = sm.OLS(design_matrix["response"], design_matrix.drop(columns="response")).fit()
            
        except (Exception, RuntimeWarning) as e:
            print("error: ", e)
            return None
    return model


def make_p_values_plot(p_sorted, theme):
    """Make p-values plot"""
    p_values = p_sorted.values
    parameters = p_sorted.index
    fig = go.Figure()
    fig.add_trace(
        {
            "x": [param.replace("*", "*<br>") for param in parameters],
            "y": p_values,
            "type": "bar",
            "marker":{"color": ["crimson" if val<0.05 else "#606060" for val in p_values]}
        }
    )
    fig["layout"].update(
        theme_layout(
            theme,
            {
                "barmode": "relative",
                "height": 500,
                "title": f"P-values for the parameters. Value lower than 0.05 is statistically significant"
            }
        )
    )
    fig.add_shape(
        {
            "type": "line", 
            "y0": 0.05, "y1": 0.05, "x0": -0.5, "x1": len(p_values)-0.5, "xref": "x",
            "line": {"color": "#303030", "width": 1.5}
        }
    )
    fig["layout"]["font"].update({"size": 12})
    return fig

def make_arrow_plot(coeff_sorted, p_sorted, theme):
    """Make arrow plot for the coefficients"""
    params_to_coefs = dict(coeff_sorted)
    p_values = p_sorted.values
    parameters = p_sorted.index
    coeff_vals = list(map(params_to_coefs.get, parameters))
    sgn = np.sign(coeff_vals)
    
    domain = 2
    steps = domain/(len(parameters)-1) if len(parameters) > 1 else 0
    num_arrows = len(parameters)
    x = np.linspace(0, domain, num_arrows) if num_arrows>1 else np.linspace(0, domain, 3)
    y = np.zeros(len(x))

    fig = px.scatter(x=x, y=y, opacity=0)
    
    fig.update_layout(
        yaxis=dict(range=[-0.15, 0.15], title='', 
                   showticklabels=False), 
        xaxis=dict(range=[-0.23, x[-1]+0.23], 
                   title='', 
                   ticktext=parameters, 
                   tickvals=[steps*i for i in range(num_arrows)] if num_arrows>1 else [1]),
        hoverlabel=dict(
            bgcolor="white", 
        )
    )
    fig.add_annotation(
        x=-0.23,
        y=0,
        text="Small <br>p-value",
        showarrow=False
    )
    fig.add_annotation(
        x=x[-1]+0.23,
        y=0,
        text="Great <br>p-value",
        showarrow=False
    )
    fig["layout"].update(
        theme_layout(
            theme,
            {
                "barmode": "relative",
                "height": 500,
                "title": "Parameters impact (increase " #Usikker på tittel (særlig det i parentes)
                         "or decrese) on response and "
                         "their significance"
            }
        )
    )
    fig["layout"]["font"].update({"size": 12})

    """Costumizing the hoverer"""
    fig.update_traces(hovertemplate='%{x}') #x is ticktext

    """Adding arrows to figure"""
    for i, s in enumerate(sgn):
        xx = x[i] if num_arrows>1 else x[1]
        fig.add_shape(
            type="path",
            path=f" M {xx-0.025} 0 " \
                 f" L {xx-0.025} {s*0.06} " \
                 f" L {xx-0.07} {s*0.06} " \
                 f" L {xx} {s*0.08} " \
                 f" L {xx+0.07} {s*0.06} " \
                 f" L {xx+0.025} {s*0.06} " \
                 f" L {xx+0.025} 0 ",
            line_color="#222A2A",
            fillcolor="crimson" if p_values[i] < 0.05 else "#606060",
            line_width=0.6  
        )
    
    """Adding zero-line along y-axis"""
    fig.add_shape(
        type="line",
        x0=-0.1,
        y0=0,
        x1=x[-1]+0.1,
        y1=0,
        line=dict(
            color='#222A2A',
            width=0.75,
        ),
    )
    return fig




def theme_layout(theme, specific_layout):
    layout = {}
    layout.update(theme["layout"])
    layout.update(specific_layout)
    return layout

@CACHE.memoize(timeout=CACHE.TIMEOUT)
@webvizstore
def read_csv(csv_file) -> pd.DataFrame:
    return pd.read_csv(csv_file, index_col=False)


