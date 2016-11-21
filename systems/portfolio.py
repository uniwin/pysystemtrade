import pandas as pd
from copy import copy

from syscore.accounting import decompose_group_pandl

from systems.stage import SystemStage
from systems.basesystem import ALL_KEYNAME
from syscore.pdutils import fix_weights_vs_pdm
from syscore.objects import update_recalc, resolve_function
from syscore.genutils import str2Bool


class Portfolios(SystemStage):
    """
    Stage for portfolios

    This is a 'switching' class which selects eithier the fixed or the estimated flavours

    """

    def __init__(self):
        setattr(self, "name", "portfolio")
        setattr(self, "description", "unswitched")

    def _system_init(self, system):
        """
        When we add this stage object to a system, this code will be run

        It will determine if we use an estimate or a fixed class of object
        """
        if str2Bool(system.config.use_instrument_weight_estimates):
            fixed_flavour = False
        else:
            fixed_flavour = True

        if fixed_flavour:
            self.__class__ = PortfoliosFixed
            self.__init__()
            setattr(self, "parent", system)

        else:
            self.__class__ = PortfoliosEstimated
            self.__init__()
            setattr(self, "parent", system)


class PortfoliosFixed(SystemStage):
    """
    Stage for portfolios

    Gets the position, accounts for instrument weights and diversification multiplier

    This version involves fixed weights and multipliers.

    Note: At this stage we're dealing with a notional, fixed, amount of capital.
         We'll need to work out p&l to scale positions properly

    KEY INPUTS: system.positionSize.get_subsystem_position(instrument_code)
                found in self.get_subsystem_position(instrument_code)

                system.positionSize.get_volatility_scalar(instrument_code)
                found in self.get_volatility_scalar

    KEY OUTPUTS: system.portfolio.get_notional_position(instrument_code)
                system.portfolio.get_buffers_for_position(instrument_code)

    Name: portfolio
    """

    def __init__(self):
        """
        Create a SystemStage for creating portfolios


        """
        protected = ["get_instrument_weights",
                     "get_instrument_diversification_multiplier", "get_raw_instrument_weights"]

        setattr(self, "_protected", protected)

        setattr(self, "name", "portfolio")
        setattr(self, "description", "fixed")

    def get_subsystem_position(self, instrument_code):
        """
        Get the position assuming all capital in one position, from a previous module

        :param instrument_code: instrument to get values for
        :type instrument_code: str

        :returns: Tx1 pd.DataFrame

        KEY INPUT

        >>> from systems.tests.testdata import get_test_object_futures_with_pos_sizing
        >>> from systems.basesystem import System
        >>> (posobject, combobject, capobject, rules, rawdata, data, config)=get_test_object_futures_with_pos_sizing()
        >>> system=System([rawdata, rules, posobject, combobject, capobject,PortfoliosFixed()], data, config)
        >>>
        >>> ## from config
        >>> system.portfolio.get_subsystem_position("EDOLLAR").tail(2)
                    ss_position
        2015-12-10     1.811465
        2015-12-11     2.544598

        """

        return self.parent.positionSize.get_subsystem_position(instrument_code)

    def get_volatility_scalar(self, instrument_code):
        """
        Get the vol scalar, from a previous module

        :param instrument_code: instrument to get values for
        :type instrument_code: str

        :returns: Tx1 pd.DataFrame

        KEY INPUT

        >>> from systems.tests.testdata import get_test_object_futures_with_pos_sizing
        >>> from systems.basesystem import System
        >>> (posobject, combobject, capobject, rules, rawdata, data, config)=get_test_object_futures_with_pos_sizing()
        >>> system=System([rawdata, rules, posobject, combobject, capobject,PortfoliosFixed()], data, config)
        >>>
        >>> ## from config
        >>> system.portfolio.get_volatility_scalar("EDOLLAR").tail(2)
                    vol_scalar
        2015-12-10   11.187869
        2015-12-11   10.332930
        """

        return self.parent.positionSize.get_volatility_scalar(instrument_code)

    def get_raw_instrument_weights(self):
        """
        Get the instrument weights

        These are 'raw' because we need to account for potentially missing positions, and weights that don't add up.

        From: (a) passed into subsystem when created
              (b) ... if not found then: in system.config.instrument_weights

        :returns: TxK pd.DataFrame containing weights, columns are instrument names, T covers all subsystem positions

        >>> from systems.tests.testdata import get_test_object_futures_with_pos_sizing
        >>> from systems.basesystem import System
        >>> (posobject, combobject, capobject, rules, rawdata, data, config)=get_test_object_futures_with_pos_sizing()
        >>> config.instrument_weights=dict(EDOLLAR=0.1, US10=0.9)
        >>> system=System([rawdata, rules, posobject, combobject, capobject,PortfoliosFixed()], data, config)
        >>>
        >>> ## from config
        >>> system.portfolio.get_instrument_weights().tail(2)
                    EDOLLAR  US10
        2015-12-10      0.1   0.9
        2015-12-11      0.1   0.9
        >>>
        >>> del(config.instrument_weights)
        >>> system2=System([rawdata, rules, posobject, combobject, capobject,PortfoliosFixed()], data, config)
        >>> system2.portfolio.get_instrument_weights().tail(2)
        WARNING: No instrument weights  - using equal weights of 0.3333 over all 3 instruments in data
                        BUND   EDOLLAR      US10
        2015-12-10  0.333333  0.333333  0.333333
        2015-12-11  0.333333  0.333333  0.333333

        """
        def _get_raw_instrument_weights(
                system, an_ignored_variable, this_stage):
            this_stage.log.msg("Calculating raw instrument weights")

            try:
                instrument_weights = system.config.instrument_weights
            except:
                instruments = self.parent.get_instrument_list()
                weight = 1.0 / len(instruments)

                warn_msg = "WARNING: No instrument weights  - using equal weights of %.4f over all %d instruments in data" % (
                    weight, len(instruments))

                this_stage.log.warn(warn_msg)

                instrument_weights = dict(
                    [(instrument_code, weight) for instrument_code in instruments])

            # Now we have a dict, fixed_weights.
            # Need to turn into a timeseries covering the range of forecast
            # dates
            instrument_list = sorted(instrument_weights.keys())

            subsys_ts = [
                this_stage.get_subsystem_position(instrument_code).index
                for instrument_code in instrument_list]

            earliest_date = min([min(fts) for fts in subsys_ts])
            latest_date = max([max(fts) for fts in subsys_ts])

            # this will be daily, but will be resampled later
            weight_ts = pd.date_range(start=earliest_date, end=latest_date)

            instrument_weights_weights = dict([
                (instrument_code, pd.Series([instrument_weights[
                 instrument_code]] * len(weight_ts), index=weight_ts))
                for instrument_code in instrument_list])

            instrument_weights_weights = pd.concat(
                instrument_weights_weights, axis=1)
            instrument_weights_weights.columns = instrument_list

            return instrument_weights_weights

        instrument_weights = self.parent.calc_or_cache(
            "get_raw_instrument_weights", ALL_KEYNAME, _get_raw_instrument_weights, self)
        return instrument_weights

    def get_instrument_weights(self):
        """
        Get the time series of instrument weights, accounting for potentially missing positions, and weights that don't add up.

        :returns: TxK pd.DataFrame containing weights, columns are instrument names, T covers all subsystem positions


        """
        def _get_instrument_weights(
                system, an_ignored_variable, this_stage):

            this_stage.log.terse("Calculating instrument weights")

            raw_instr_weights = this_stage.get_raw_instrument_weights()
            instrument_list = list(raw_instr_weights.columns)

            subsys_positions = [this_stage.get_subsystem_position(instrument_code)
                                for instrument_code in instrument_list]

            subsys_positions = pd.concat(subsys_positions, axis=1).ffill()
            subsys_positions.columns = instrument_list

            instrument_weights = fix_weights_vs_pdm(
                raw_instr_weights, subsys_positions)

            weighting = system.config.instrument_weight_ewma_span

            # smooth
            instrument_weights = pd.ewma(instrument_weights, weighting)

            return instrument_weights

        instrument_weights = self.parent.calc_or_cache(
            "get_instrument_weights", ALL_KEYNAME, _get_instrument_weights, self)
        return instrument_weights

    def get_instrument_diversification_multiplier(self):
        """
        Get the instrument diversification multiplier

        :returns: TxK pd.DataFrame containing weights, columns are instrument names, T covers all subsystem positions

        >>> from systems.tests.testdata import get_test_object_futures_with_pos_sizing
        >>> from systems.basesystem import System
        >>> (posobject, combobject, capobject, rules, rawdata, data, config)=get_test_object_futures_with_pos_sizing()
        >>> system=System([rawdata, rules, posobject, combobject, capobject,PortfoliosFixed()], data, config)
        >>>
        >>> ## from config
        >>> system.portfolio.get_instrument_diversification_multiplier().tail(2)
                    idm
        2015-12-10  1.2
        2015-12-11  1.2
        >>>
        >>> ## from defaults
        >>> del(config.instrument_div_multiplier)
        >>> system2=System([rawdata, rules, posobject, combobject, capobject,PortfoliosFixed()], data, config)
        >>> system2.portfolio.get_instrument_diversification_multiplier().tail(2)
                    idm
        2015-12-10    1
        2015-12-11    1
        """
        def _get_instrument_div_multiplier(
                system, an_ignored_variable, this_stage):

            this_stage.log.terse("Calculating diversification multiplier")

            div_mult = system.config.instrument_div_multiplier

            # Now we have a fixed weight
            # Need to turn into a timeseries covering the range of forecast
            # dates

            # this will be daily, but will be resampled later
            weight_ts = this_stage.get_instrument_weights().index

            ts_idm = pd.Series([div_mult] * len(weight_ts),
                               index=weight_ts)

            return ts_idm

        instrument_div_multiplier = self.parent.calc_or_cache(
            "get_instrument_diversification_multiplier", ALL_KEYNAME, _get_instrument_div_multiplier, self)
        return instrument_div_multiplier

    def get_notional_position(self, instrument_code):
        """
        Gets the position, accounts for instrument weights and diversification multiplier

        Note: At this stage we're dealing with a notional, fixed, amount of capital.
             We'll need to work out p&l to scale positions properly

        :param instrument_code: instrument to get values for
        :type instrument_code: str

        :returns: Tx1 pd.DataFrame

        KEY OUTPUT
        >>> from systems.tests.testdata import get_test_object_futures_with_pos_sizing
        >>> from systems.basesystem import System
        >>> (posobject, combobject, capobject, rules, rawdata, data, config)=get_test_object_futures_with_pos_sizing()
        >>> system=System([rawdata, rules, posobject, combobject, capobject,PortfoliosFixed()], data, config)
        >>>
        >>> ## from config
        >>> system.portfolio.get_notional_position("EDOLLAR").tail(2)
                         pos
        2015-12-10  1.086879
        2015-12-11  1.526759

        """
        def _get_notional_position(system, instrument_code, this_stage):

            this_stage.log.msg("Calculating notional position for %s" % instrument_code,
                               instrument_code=instrument_code)

            idm = this_stage.get_instrument_diversification_multiplier()
            instr_weights = this_stage.get_instrument_weights()
            subsys_position = this_stage.get_subsystem_position(
                instrument_code)

            inst_weight_this_code = instr_weights[
                instrument_code]

            inst_weight_this_code = inst_weight_this_code.reindex(
                subsys_position.index).ffill()
            idm = idm.reindex(subsys_position.index).ffill()

            notional_position = subsys_position * inst_weight_this_code * idm

            return notional_position

        notional_position = self.parent.calc_or_cache(
            "get_notional_position", instrument_code, _get_notional_position, self)
        return notional_position

    def get_position_method_buffer(self, instrument_code):
        """
        Gets the buffers for positions, using proportion of position method

        :param instrument_code: instrument to get values for
        :type instrument_code: str

        :returns: Tx1 pd.DataFrame

        >>> from systems.tests.testdata import get_test_object_futures_with_pos_sizing
        >>> from systems.basesystem import System
        >>> (posobject, combobject, capobject, rules, rawdata, data, config)=get_test_object_futures_with_pos_sizing()
        >>> system=System([rawdata, rules, posobject, combobject, capobject,PortfoliosFixed()], data, config)
        >>>
        >>> ## from config
        >>> system.portfolio.get_position_method_buffer("EDOLLAR").tail(2)
                      buffer
        2015-12-10  0.108688
        2015-12-11  0.152676
        """
        def _get_position_method_buffer(system, instrument_code, this_stage):

            this_stage.log.msg("Calculating position method buffer for %s" % instrument_code,
                               instrument_code=instrument_code)

            buffer_size = system.config.buffer_size

            position = this_stage.get_notional_position(instrument_code)

            buffer = position * buffer_size

            buffer.columns = ["buffer"]

            return buffer

        buffer = self.parent.calc_or_cache(
            "get_position_method_buffer", instrument_code, _get_position_method_buffer, self)

        return buffer

    def get_forecast_method_buffer(self, instrument_code):
        """
        Gets the buffers for positions, using proportion of average forecast method


        :param instrument_code: instrument to get values for
        :type instrument_code: str

        :returns: Tx1 pd.DataFrame

        >>> from systems.tests.testdata import get_test_object_futures_with_pos_sizing
        >>> from systems.basesystem import System
        >>> (posobject, combobject, capobject, rules, rawdata, data, config)=get_test_object_futures_with_pos_sizing()
        >>> system=System([rawdata, rules, posobject, combobject, capobject,PortfoliosFixed()], data, config)
        >>>
        >>> ## from config
        >>> system.portfolio.get_forecast_method_buffer("EDOLLAR").tail(2)
                      buffer
        2015-12-10  0.671272
        2015-12-11  0.619976
        """
        def _get_forecast_method_buffer(system, instrument_code, this_stage):

            this_stage.log.msg("Calculating forecast method buffers for %s" % instrument_code,
                               instrument_code=instrument_code)

            buffer_size = system.config.buffer_size
            position = this_stage.get_notional_position(instrument_code)

            idm = this_stage.get_instrument_diversification_multiplier()
            instr_weights = this_stage.get_instrument_weights()
            vol_scalar = this_stage.get_volatility_scalar(
                instrument_code)

            inst_weight_this_code = instr_weights[
                instrument_code]

            inst_weight_this_code = inst_weight_this_code.reindex(
                position.index).ffill()
            idm = idm.reindex(position.index).ffill()
            vol_scalar = vol_scalar.reindex(position.index).ffill()

            average_position = vol_scalar * inst_weight_this_code * idm

            buffer = average_position * buffer_size

            return buffer

        buffer = self.parent.calc_or_cache(
            "get_forecast_method_buffer", instrument_code, _get_forecast_method_buffer, self)

        return buffer

    def get_buffers_for_position(self, instrument_code):
        """
        Gets the buffers for positions, using method depending on config.buffer_method

        KEY OUTPUT

        :param instrument_code: instrument to get values for
        :type instrument_code: str

        :returns: Tx2 pd.DataFrame

        >>> from systems.tests.testdata import get_test_object_futures_with_pos_sizing
        >>> from systems.basesystem import System
        >>> (posobject, combobject, capobject, rules, rawdata, data, config)=get_test_object_futures_with_pos_sizing()
        >>> system=System([rawdata, rules, posobject, combobject, capobject,PortfoliosFixed()], data, config)
        >>>
        >>> ## from config
        >>> system.portfolio.get_buffers_for_position("EDOLLAR").tail(2)
                     top_pos   bot_pos
        2015-12-10  1.195567  0.978191
        2015-12-11  1.679435  1.374083
        """
        def _get_buffers_for_position(system, instrument_code, this_stage):

            this_stage.log.msg("Calculating buffers for %s" % instrument_code,
                               instrument_code=instrument_code)

            buffer_method = system.config.buffer_method

            if buffer_method == "forecast":
                buffer = this_stage.get_forecast_method_buffer(instrument_code)
            elif buffer_method == "position":
                buffer = this_stage.get_position_method_buffer(instrument_code)
            else:
                this_stage.log.critical(
                    "Buffer method %s not recognised - not buffering" %
                    buffer_method)
                position = this_stage.get_notional_position(instrument_code)
                max_max_position = float(position.abs().max()) * 10.0
                buffer = pd.Series(
                    [max_max_position] *
                    position.shape[0],
                    index=position.index)

            position = this_stage.get_notional_position(instrument_code)

            top_position = position + buffer.ffill()

            bottom_position = position - buffer.ffill()

            pos_buffers = pd.concat([top_position, bottom_position], axis=1)
            pos_buffers.columns = ["top_pos", "bot_pos"]

            return pos_buffers

        pos_buffers = self.parent.calc_or_cache(
            "get_buffers_for_position", instrument_code, _get_buffers_for_position, self)

        return pos_buffers

    def capital_multiplier(self):
        return self.parent.accounts.capital_multiplier()

    def get_actual_position(self, instrument_code):
        """
        Gets the actual position, accounting for cap multiplier

        :param instrument_code: instrument to get values for
        :type instrument_code: str

        :returns: Tx1 pd.Series

        KEY OUTPUT
        """
        def _get_actual_position(system, instrument_code, this_stage):

            this_stage.log.msg("Calculating actual position for %s" % instrument_code,
                               instrument_code=instrument_code)

            notional_position = this_stage.get_notional_position(
                instrument_code)
            cap_multiplier = this_stage.capital_multiplier()
            cap_multiplier = cap_multiplier.reindex(
                notional_position.index).ffill()

            actual_position = notional_position * cap_multiplier

            return actual_position

        actual_position = self.parent.calc_or_cache(
            "get_actual_position", instrument_code, _get_actual_position, self)
        return actual_position

    def get_actual_buffers_for_position(self, instrument_code):
        """
        Gets the actual buffers for a position, accounting for cap multiplier

        :param instrument_code: instrument to get values for
        :type instrument_code: str

        :returns: Tx1 pd.Series

        KEY OUTPUT
        """
        def _get_actual_buffers_for_position(
                system, instrument_code, this_stage):

            this_stage.log.msg("Calculating actual buffers for position for %s" % instrument_code,
                               instrument_code=instrument_code)

            buffers = this_stage.get_buffers_for_position(instrument_code)
            cap_multiplier = this_stage.capital_multiplier()
            cap_multiplier = cap_multiplier.reindex(buffers.index).ffill()
            cap_multiplier = pd.concat(
                [cap_multiplier, cap_multiplier], axis=1)
            cap_multiplier.columns = buffers.columns

            actual_buffers_for_position = buffers * cap_multiplier

            return actual_buffers_for_position

        actual_position = self.parent.calc_or_cache(
            "get_actual_buffers_for_position", instrument_code, _get_actual_buffers_for_position, self)
        return actual_position


class PortfoliosEstimated(PortfoliosFixed):
    """
    Stage for portfolios

    This version involves estimated weights and multipliers.

    Name: portfolio

    KEY INPUTS: as per parent class, plus:

                system.accounts.pandl_across_subsystems
                found in: self.pandl_across_subsystems

    KEY OUTPUTS: No additional outputs


    """

    def __init__(self):

        super(PortfoliosEstimated, self).__init__()

        """
        if you add another method to this you also need to add its blank dict here
        """

        protected = ['get_instrument_correlation_matrix']
        update_recalc(self, protected)

        setattr(self, "description", "Estimated")

        nopickle = ["calculation_of_raw_instrument_weights"]
        setattr(self, "_nopickle", nopickle)

    def get_instrument_subsystem_SR_cost(self, instrument_code):
        """
        Get the SR cost of a subsystem

        :param instrument_code: instrument to get values for
        :type instrument_code: str

        :returns: float

        KEY INPUT
        """

        # do not round positions as will over inflate costs for small accounts
        return self.parent.accounts.subsystem_SR_costs(
            instrument_code, roundpositions=False)

    def get_instrument_correlation_matrix(self):
        """
        Returns a correlationList object which contains a history of correlation matricies

        :returns: correlation_list object

        >>> from systems.tests.testdata import get_test_object_futures_with_pos_sizing_estimates
        >>> from systems.basesystem import System
        >>> (account, posobject, combobject, capobject, rules, rawdata, data, config)=get_test_object_futures_with_pos_sizing_estimates()
        >>> system=System([rawdata, rules, posobject, combobject, capobject,PortfoliosEstimated(), account], data, config)
        >>> system.config.forecast_weight_estimate["method"]="shrinkage" ## speed things up
        >>> system.config.forecast_weight_estimate["date_method"]="in_sample" ## speed things up
        >>> system.config.instrument_weight_estimate["date_method"]="in_sample" ## speed things up
        >>> system.config.instrument_weight_estimate["method"]="shrinkage" ## speed things up
        >>> ans=system.portfolio.get_instrument_correlation_matrix()
        >>> ans.corr_list[-1]
        array([[ 1.        ,  0.56981346,  0.62458477],
               [ 0.56981346,  1.        ,  0.88087893],
               [ 0.62458477,  0.88087893,  1.        ]])
        >>> print(ans.corr_list[0])
        [[ 1.    0.99  0.99]
         [ 0.99  1.    0.99]
         [ 0.99  0.99  1.  ]]
        >>> print(ans.corr_list[10])
        [[ 1.          0.99        0.99      ]
         [ 0.99        1.          0.78858156]
         [ 0.99        0.78858156  1.        ]]
        """

        def _get_instrument_correlation_matrix(system, NotUsed, this_stage,
                                               corr_func, **corr_params):

            this_stage.log.terse("Calculating instrument correlations")

            instrument_codes = system.get_instrument_list()

            if hasattr(system, "accounts"):
                pandl = this_stage.pandl_across_subsystems().to_frame()
            else:
                error_msg = "You need an accounts stage in the system to estimate instrument correlations"
                this_stage.log.critical(error_msg)

            # Need to resample here, because the correlation function won't do
            # it properly
            frequency = corr_params['frequency']
            pandl = pandl.cumsum().resample(frequency).diff()

            return corr_func(pandl, log=this_stage.log.setup(
                call="correlation"), **corr_params)

        # Get some useful stuff from the config
        corr_params = copy(self.parent.config.instrument_correlation_estimate)

        # which function to use for calculation
        corr_func = resolve_function(corr_params.pop("func"))

        # _get_instrument_correlation_matrix: function to call if we don't find in cache
        # self: this_system stage object
        # func: function to call to calculate correlations
        # **corr_params: parameters to pass to correlation function
        ##

        forecast_corr_list = self.parent.calc_or_cache(
            'get_instrument_correlation_matrix', ALL_KEYNAME,
            _get_instrument_correlation_matrix,
            self, corr_func, **corr_params)

        return forecast_corr_list

    def get_instrument_diversification_multiplier(self):
        """

        Estimate the diversification multiplier for the portfolio

        Estimated from correlations and weights

        :returns: Tx1 pd.DataFrame

        >>> from systems.tests.testdata import get_test_object_futures_with_pos_sizing_estimates
        >>> from systems.basesystem import System
        >>> (account, posobject, combobject, capobject, rules, rawdata, data, config)=get_test_object_futures_with_pos_sizing_estimates()
        >>> system=System([rawdata, rules, posobject, combobject, capobject,PortfoliosEstimated(), account], data, config)
        >>> system.config.forecast_weight_estimate["method"]="shrinkage" ## speed things up
        >>> system.config.forecast_weight_estimate["date_method"]="in_sample" ## speed things up
        >>> system.config.instrument_weight_estimate["date_method"]="in_sample" ## speed things up
        >>> system.config.instrument_weight_estimate["method"]="shrinkage" ## speed things up
        >>> system.portfolio.get_instrument_diversification_multiplier().tail(3)
                         IDM
        2015-12-09  1.133220
        2015-12-10  1.133186
        2015-12-11  1.133153
        """
        def _get_instrument_div_multiplier(system, NotUsed, this_stage):

            this_stage.log.terse("Calculating instrument div. multiplier")

            # Get some useful stuff from the config
            div_mult_params = copy(system.config.instrument_div_mult_estimate)

            idm_func = resolve_function(div_mult_params.pop("func"))

            correlation_list_object = this_stage.get_instrument_correlation_matrix()
            weight_df = this_stage.get_instrument_weights()

            ts_idm = idm_func(
                correlation_list_object,
                weight_df,
                **div_mult_params)

            return ts_idm

        instrument_div_multiplier = self.parent.calc_or_cache(
            'get_instrument_diversification_multiplier', ALL_KEYNAME, _get_instrument_div_multiplier,
            self)
        return instrument_div_multiplier

    def get_raw_instrument_weights(self):
        """
        Estimate the instrument weights


        :returns: TxK pd.DataFrame containing weights, columns are trading rule variation names, T covers all

        >>> from systems.tests.testdata import get_test_object_futures_with_pos_sizing_estimates
        >>> from systems.basesystem import System
        >>> (account, posobject, combobject, capobject, rules, rawdata, data, config)=get_test_object_futures_with_pos_sizing_estimates()
        >>> system=System([account, rawdata, rules, posobject, combobject, capobject,PortfoliosEstimated()], data, config)
        >>> system.config.forecast_weight_estimate["method"]="shrinkage" ## speed things up
        >>> system.config.forecast_weight_estimate["date_method"]="in_sample" ## speed things up
        >>> system.config.instrument_weight_estimate["method"]="shrinkage"
        >>> system.portfolio.get_raw_instrument_weights().tail(3)
                            BUND   EDOLLAR      US10
        2015-05-30  4.006172e-17  0.499410  0.500590
        2015-06-01  5.645388e-01  0.217462  0.217999
        2015-12-12  5.645388e-01  0.217462  0.217999
        """

        def _get_raw_instrument_weights(system, notUsed, this_stage):
            this_stage.log.msg("Getting raw instrument weights")

            return this_stage.calculation_of_raw_instrument_weights().weights

        ##
        raw_instrument_weights = self.parent.calc_or_cache(
            'get_raw_instrument_weights', ALL_KEYNAME,
            _get_raw_instrument_weights,
            self)

        return raw_instrument_weights

        instrument_weights = self.parent.calc_or_cache(
            'get_instrument_weights', ALL_KEYNAME, _get_raw_instrument_weights, self)
        return instrument_weights

    def pandl_across_subsystems(self):
        """
        Return profitability of each instrument

        KEY INPUT

        :param instrument_code:
        :type str:

        :returns: accountCurveGroup object
        """

        return self.parent.accounts.pandl_across_subsystems()

    def _get_all_subsystem_positions(self):
        instrument_codes = self.parent.get_instrument_list()

        positions = [self.get_subsystem_position(
            instr_code) for instr_code in instrument_codes]
        positions = pd.concat(positions, axis=1)
        positions.columns = instrument_codes

        return positions

    def calculation_of_raw_instrument_weights(self):
        """
        Estimate the instrument weights

        Done like this to expose calculations

        :returns: TxK pd.DataFrame containing weights, columns are instrument names, T covers all

        """

        def _calculation_of_raw_instrument_weights(system, NotUsed1, this_stage,
                                                   weighting_func, **weighting_params):

            this_stage.log.terse("Calculating raw instrument weights")

            instrument_codes = system.get_instrument_list()

            weight_func = weighting_func(
                log=this_stage.log.setup(
                    call="weighting"),
                **weighting_params)
            if weight_func.need_data():

                if hasattr(system, "accounts"):
                    pandl = this_stage.pandl_across_subsystems()
                    (pandl_gross, pandl_costs) = decompose_group_pandl([pandl])

                    weight_func.set_up_data(
                        data_gross=pandl_gross, data_costs=pandl_costs)

                else:
                    error_msg = "You need an accounts stage in the system to estimate instrument weights"
                    this_stage.log.critical(error_msg)

            else:
                # equal weights doesn't need data

                positions = this_stage._get_all_subsystem_positions()
                weight_func.set_up_data(weight_matrix=positions)

            SR_cost_list = [this_stage.get_instrument_subsystem_SR_cost(
                instr_code) for instr_code in instrument_codes]

            weight_func.optimise(ann_SR_costs=SR_cost_list)

            return weight_func

        # Get some useful stuff from the config
        weighting_params = copy(self.parent.config.instrument_weight_estimate)

        # which function to use for calculation
        weighting_func = resolve_function(weighting_params.pop("func"))

        calcs_of_instrument_weights = self.parent.calc_or_cache(
            'calculation_of_raw_instrument_weights', ALL_KEYNAME,
            _calculation_of_raw_instrument_weights,
            self, weighting_func, **weighting_params)

        return calcs_of_instrument_weights


if __name__ == '__main__':
    import doctest
    doctest.testmod()
